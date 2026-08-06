import pytest
from pydantic import ValidationError

from workbench.models.profiles import ProviderCapability, ProviderProfileRecord


def test_profile_persists_secret_reference_and_deepseek_capabilities() -> None:
    """A stored profile must carry an opaque reference, never credential material."""
    profile = ProviderProfileRecord.deepseek(
        id="deepseek-primary",
        secret_id="provider/deepseek-primary",
        reasoning_effort="max",
    )

    persisted = profile.model_dump()

    assert profile.base_url == "https://api.deepseek.com"
    assert profile.model_aliases["default"] == "deepseek-v4-flash"
    assert profile.reasoning_effort == "max"
    assert ProviderCapability.THINKING in profile.capabilities
    assert ProviderCapability.TOOL_CALLING in profile.capabilities
    assert persisted["secret_id"] == "provider/deepseek-primary"
    assert "api_key" not in persisted


def test_profile_rejects_plaintext_credential_fields() -> None:
    """Adding a credential value to serializable profile state is invalid."""
    with pytest.raises(ValidationError):
        ProviderProfileRecord(
            id="deepseek-primary",
            name="DeepSeek",
            protocol="deepseek",
            base_url="https://api.deepseek.com",
            api_key="must-not-be-stored",
        )


@pytest.mark.parametrize(
    "header_name",
    ["aUtHoRiZaTiOn", "PrOxY_AuThOrIzAtIoN", "X API Key"],
)
def test_profile_rejects_credential_headers_regardless_of_case_or_separator(
    header_name: str,
) -> None:
    """Serialized provider metadata must not be a second secret store."""
    with pytest.raises(ValidationError):
        ProviderProfileRecord(
            id="custom-provider",
            name="Custom Provider",
            protocol="openai_chat",
            base_url="https://provider.test",
            headers={header_name: "plaintext-credential"},
        )
