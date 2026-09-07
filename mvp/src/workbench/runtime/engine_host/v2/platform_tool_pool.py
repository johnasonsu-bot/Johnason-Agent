"""Compose existing Host ToolRouter executors for Goose and DSH.

This module has no model client or user workspace authority. Frozen context is
supplied by the admission repository; bodies use the existing ArtifactStore.
"""
from __future__ import annotations

from pathlib import Path
import secrets
import threading

from workbench.artifacts.store import ArtifactStore
from workbench.credentials.service import VaultService
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import HmacRequestDigestService, ToolAccess, ToolRouter
from workbench.workflow.store import WorkflowStore

from .artifact_tools import ARTIFACT_TOOL_MANIFEST, ArtifactTools
from .platform_tools import FrozenPlatformContextFactory, PlatformToolRouterAdapter
from .python_term_control_plane import _build_registry, _declare_executor
from .registry import RuntimeRegistryV2
from .repository import RuntimeV2Repository
from .tool_transport import PlatformToolCall, PlatformToolResult

_KEY_INITIALIZATION_LOCK = threading.RLock()


class StableToolDigestKey:
    """Retain the existing ToolRouter request identity across app restarts.

Only a reference is stored in SQLite. The internal key stays in the same encrypted
Vault as other local secrets, not in settings, source, an envelope or a grant.
"""
    SECRET_ID = "internal.runtime-tool-request-digest.v1"

    def __init__(self, vault: VaultService, database: Path) -> None:
        self._vault = vault
        self._store = WorkflowStore(database)
        with self._store.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_tool_digest_reference ("
                "secret_ref TEXT PRIMARY KEY)"
            )

    def service(self) -> HmacRequestDigestService:
        with _KEY_INITIALIZATION_LOCK:
            with self._store.connect() as connection:
                initialized = connection.execute(
                    "SELECT 1 FROM runtime_tool_digest_reference WHERE secret_ref = ?",
                    (self.SECRET_ID,),
                ).fetchone() is not None
            try:
                encoded = self._vault.get(self.SECRET_ID)
            except KeyError:
                if initialized:
                    raise RuntimeError("previous Tool request identity is unavailable") from None
                encoded = secrets.token_hex(32)
                self._vault.put(self.SECRET_ID, encoded)
            try:
                key = bytes.fromhex(encoded)
                if len(encoded) != 64 or len(key) != 32:
                    raise ValueError()
            except (TypeError, ValueError):
                raise RuntimeError("Tool request identity is unavailable") from None
            with self._store.connect() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO runtime_tool_digest_reference(secret_ref) VALUES (?)",
                    (self.SECRET_ID,),
                )
            return HmacRequestDigestService(key)


class PlatformToolPool:
    """One trusted ToolRouter per runtime, sharing the platform database."""
    def __init__(
        self, *, database: Path, artifact_root: Path, vault: VaultService,
        context_factory: FrozenPlatformContextFactory, owner_id: str,
    ) -> None:
        if not callable(context_factory) or not isinstance(owner_id, str) or not owner_id:
            raise ValueError("platform Tool context and owner are required")
        self._database = database
        self._context_factory = context_factory
        self._owner_id = owner_id
        self._repository = PythonTermRepository(database)
        self._artifacts = ArtifactTools(ArtifactStore(database, artifact_root))
        self._key = StableToolDigestKey(vault, database)
        self._adapters: dict[str, PlatformToolRouterAdapter] = {}
        self._lock = threading.RLock()

    def _adapter(self, runtime_id: str) -> PlatformToolRouterAdapter:
        if runtime_id not in {"goose", "dsh"}:
            raise ValueError("platform Tool runtime is unsupported")
        with self._lock:
            existing = self._adapters.get(runtime_id)
            if existing is not None:
                return existing
            # Fail while Vault is locked; do not manufacture an ephemeral key.
            # A live router retains its key while settling in-flight operations.
            digest_service = self._key.service()
            registry = RuntimeRegistryV2(RuntimeV2Repository(self._database))
            implementations = (self._artifacts.publish, self._artifacts.read)
            descriptors = tuple(
                _declare_executor(
                    registry, runtime_id, manifest, f"{manifest.tool_id}.v1",
                    ToolAccess(), implementation,
                )
                for manifest, implementation in zip(ARTIFACT_TOOL_MANIFEST, implementations, strict=True)
            )
            broker, registrations = _build_registry(registry, descriptors, 8)
            router = ToolRouter(
                self._repository, registrations, executor_broker=broker,
                request_digests=digest_service,
            )
            adapter = PlatformToolRouterAdapter(
                runtime_id=runtime_id, router=router, repository=self._repository,
                context_factory=self._context_factory,
                owner_id=f"{self._owner_id}:{runtime_id}",
            )
            self._adapters[runtime_id] = adapter
            return adapter

    async def __call__(self, call: PlatformToolCall) -> PlatformToolResult:
        return await self._adapter(call.envelope.runtime.runtime_id)(call)
