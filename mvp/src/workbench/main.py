"""Runnable local API entrypoint."""

import uvicorn
from fastapi import FastAPI

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.credentials.service import VaultService
from workbench.models.deepseek import DeepSeekProvider
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.openai_compatible import OpenAICompatibleProvider
from workbench.settings import WorkbenchSettings


class IdleRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult(checkpoint={"runner": "idle"})


def build_app(settings: WorkbenchSettings | None = None) -> FastAPI:
    resolved = settings or WorkbenchSettings()
    resolved.runtime_dir.mkdir(parents=True, exist_ok=True)
    vault = VaultService(resolved.vault_path)
    gateway = ModelGateway(
        {
            "lmstudio": LMStudioProvider(resolved.local_model_base_url),
            "deepseek": DeepSeekProvider(vault=vault),
            "openai_compatible": OpenAICompatibleProvider(vault=vault),
            "openai_chat": OpenAICompatibleProvider(vault=vault),
        }
    )
    return create_app(
        AppSettings(
            database=resolved.database,
            runner=IdleRunner(),
            owner_id=resolved.owner_id,
            vault=vault,
            gateway=gateway,
            close_gateway=True,
        )
    )


def main() -> None:
    settings = WorkbenchSettings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
