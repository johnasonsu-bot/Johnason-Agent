from workbench.validation.result import ValidationResult, ValidationStatus
from workbench.validation.runner import decision_code, decision_name


REQUIRED = {"a", "b"}


def _result(check: str, status: ValidationStatus) -> ValidationResult:
    return ValidationResult(check=check, status=status)


def test_all_required_pass_returns_zero() -> None:
    results = [
        _result("a", ValidationStatus.PASS),
        _result("b", ValidationStatus.PASS),
    ]
    assert decision_code(results, REQUIRED) == 0
    assert decision_name(results, REQUIRED) == "GO_PHASE_1"


def test_any_failure_returns_one() -> None:
    results = [
        _result("a", ValidationStatus.PASS),
        _result("b", ValidationStatus.FAIL),
    ]
    assert decision_code(results, REQUIRED) == 1
    assert decision_name(results, REQUIRED) == "BLOCKED"


def test_required_blocked_returns_two() -> None:
    results = [
        _result("a", ValidationStatus.PASS),
        _result("b", ValidationStatus.BLOCKED),
    ]
    assert decision_code(results, REQUIRED) == 2
    assert decision_name(results, REQUIRED) == "BLOCKED"


def test_optional_blocked_allows_go_with_degradation() -> None:
    results = [
        _result("a", ValidationStatus.PASS),
        _result("b", ValidationStatus.PASS),
        _result("optional", ValidationStatus.BLOCKED),
    ]
    assert decision_code(results, REQUIRED) == 0
    assert decision_name(results, REQUIRED) == "GO_WITH_DEGRADATION"


def test_missing_required_check_is_blocked() -> None:
    assert decision_code([_result("a", ValidationStatus.PASS)], REQUIRED) == 2
