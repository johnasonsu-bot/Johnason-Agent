"""Live LM Studio tool-calling validation."""

from workbench.models.contracts import ModelRequest, ToolDefinition
from workbench.models.lmstudio import (
    LMStudioProvider,
    ProviderResponseError,
    ProviderUnavailable,
)
from workbench.validation.result import (
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)


async def probe_lmstudio(base_url: str, model_id: str | None) -> ValidationResult:
    provider = LMStudioProvider(base_url)
    try:
        models = await provider.list_models()
        if not models:
            return _blocked("LM Studio is reachable but no model is loaded", base_url)
        if not model_id:
            return _blocked(
                "LMSTUDIO_MODEL is not configured",
                base_url,
                loaded_models=",".join(models),
            )
        if model_id not in models:
            return _blocked(
                f"Configured model {model_id} is not loaded",
                base_url,
                loaded_models=",".join(models),
            )

        response = await provider.complete_with_tools(
            ModelRequest(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Call phase0_echo exactly once with value set to ok. "
                            "Do not answer with plain text."
                        ),
                    }
                ],
                tools=[
                    ToolDefinition(
                        name="phase0_echo",
                        description="Echo the validation value",
                        parameters={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    )
                ],
            )
        )
    except ProviderUnavailable as exc:
        return _blocked(f"LM Studio is unavailable: {exc}", base_url)
    except ProviderResponseError as exc:
        return ValidationResult(
            check="lmstudio.tool_calling",
            status=ValidationStatus.FAIL,
            summary=f"LM Studio returned an invalid response: {exc}",
        )
    finally:
        await provider.aclose()

    matching = [
        call
        for call in response.tool_calls
        if call.name == "phase0_echo" and call.arguments == {"value": "ok"}
    ]
    if not matching:
        return ValidationResult(
            check="lmstudio.tool_calling",
            status=ValidationStatus.FAIL,
            summary="The selected model did not produce the required tool call",
            evidence=[ValidationEvidence(name="model", value=model_id)],
        )
    return ValidationResult(
        check="lmstudio.tool_calling",
        status=ValidationStatus.PASS,
        summary="LM Studio produced the required tool call",
        evidence=[
            ValidationEvidence(name="base_url", value=base_url),
            ValidationEvidence(name="model", value=model_id),
        ],
    )


def _blocked(
    summary: str,
    base_url: str,
    *,
    loaded_models: str | None = None,
) -> ValidationResult:
    evidence = [ValidationEvidence(name="base_url", value=base_url)]
    if loaded_models:
        evidence.append(ValidationEvidence(name="loaded_models", value=loaded_models))
    return ValidationResult(
        check="lmstudio.tool_calling",
        status=ValidationStatus.BLOCKED,
        summary=summary,
        evidence=evidence,
    )

