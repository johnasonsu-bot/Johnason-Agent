"""Runnable local API entrypoint."""

import uvicorn
from fastapi import FastAPI

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.settings import WorkbenchSettings


class IdleRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult(checkpoint={"runner": "idle"})


def build_app(settings: WorkbenchSettings | None = None) -> FastAPI:
    resolved = settings or WorkbenchSettings()
    resolved.runtime_dir.mkdir(parents=True, exist_ok=True)
    return create_app(
        AppSettings(
            database=resolved.database,
            runner=IdleRunner(),
            owner_id=resolved.owner_id,
        )
    )


def main() -> None:
    settings = WorkbenchSettings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
