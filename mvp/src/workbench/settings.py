"""Local settings containing paths and credential references only."""

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from workbench.runtime.engine_host.v2.security import validate_runtime_argv


class RuntimeProcessConfig(BaseModel):
    """Structured process identity only; execution and environment stay outside settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    argv: Annotated[tuple[StrictStr, ...], Field(min_length=1)]

    @field_validator("runtime_id")
    @classmethod
    def validate_runtime_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runtime_id must not be blank")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_runtime_argv(value)


class WorkbenchSettings(BaseModel):
    runtime_dir: Path = Path(".runtime")
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=0, le=65535)
    owner_id: str = "local-workbench"
    local_model_base_url: str = "http://127.0.0.1:1234"
    openai_credential_env: str = "OPENAI_API_KEY"
    anthropic_credential_env: str = "ANTHROPIC_API_KEY"
    data_platform_credential_env: str = "DATA_PLATFORM_TOKEN"
    engine_host_enabled: bool = False
    engine_host_command: tuple[str, ...] = ()
    engine_host_provider_allowlist: tuple[str, ...] = ("lmstudio",)
    engine_host_v2_enabled: bool = False
    engine_host_v2_runtimes: tuple[RuntimeProcessConfig, ...] = ()
    python_term_runtime_enabled: StrictBool = False

    @property
    def database(self) -> Path:
        return self.runtime_dir / "workbench.sqlite"

    @property
    def vault_path(self) -> Path:
        return self.runtime_dir / "credentials.vault"

    @property
    def artifact_root(self) -> Path:
        return self.runtime_dir / "artifacts"
