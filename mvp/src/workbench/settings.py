"""Local settings containing paths and credential references only."""

from pathlib import Path

from pydantic import BaseModel, Field


class WorkbenchSettings(BaseModel):
    runtime_dir: Path = Path(".runtime")
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    owner_id: str = "local-workbench"
    local_model_base_url: str = "http://127.0.0.1:1234"
    openai_credential_env: str = "OPENAI_API_KEY"
    anthropic_credential_env: str = "ANTHROPIC_API_KEY"
    data_platform_credential_env: str = "DATA_PLATFORM_TOKEN"

    @property
    def database(self) -> Path:
        return self.runtime_dir / "workbench.sqlite"

    @property
    def artifact_root(self) -> Path:
        return self.runtime_dir / "artifacts"
