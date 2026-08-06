from workbench.validation.result import ValidationResult, ValidationStatus


def test_validation_result_serializes_stable_status() -> None:
    result = ValidationResult(
        check="lmstudio.health",
        status=ValidationStatus.PASS,
    )

    assert result.model_dump(mode="json")["status"] == "pass"

