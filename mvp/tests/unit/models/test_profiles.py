import pytest
from pydantic import ValidationError

from workbench.models.profiles import ProviderCapability, ProviderProfileRecord, SafeHeaders


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
    [
        "aUtHoRiZaTiOn",
        "PrOxY_AuThOrIzAtIoN",
        "X API Key",
        "Cookie",
        "X-Api-Token",
        "X-Client-Secret",
        "Ocp-Apim-Subscription-Key",
        "X-Unrelated-Metadata",
    ],
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


def test_profile_allows_only_safe_custom_metadata_headers() -> None:
    """Non-secret request metadata remains configurable and serializable."""
    headers = {
        "accept": "application/json",
        "Content Type": "application/json",
        "User-Agent": "workbench",
        "HTTP Referer": "https://app.test",
        "X-Title": "Workbench",
    }

    profile = ProviderProfileRecord(
        id="custom-provider",
        name="Custom Provider",
        protocol="openai_chat",
        base_url="https://provider.test",
        headers=headers,
    )

    assert profile.model_dump()["headers"] == headers


def test_headers_are_immutable_and_dict_profile_exposes_only_safe_mapping() -> None:
    """Normal mapping mutations must not create an unsafe profile state."""
    profile = ProviderProfileRecord(
        id="custom-provider",
        name="Custom Provider",
        protocol="openai_chat",
        base_url="https://provider.test",
        headers={"Accept": "application/json"},
    )

    with pytest.raises(TypeError, match="immutable"):
        profile.headers["Cookie"] = "session=plaintext"
    with pytest.raises(TypeError, match="immutable"):
        profile.headers.update({"X-Api-Token": "plaintext-token"})
    with pytest.raises(TypeError, match="immutable"):
        profile.headers.pop("Accept")
    with pytest.raises(TypeError, match="immutable"):
        profile.headers.clear()

    exposed = dict(profile)["headers"]
    assert isinstance(exposed, SafeHeaders)
    assert dict(exposed) == {"Accept": "application/json"}
    assert profile.model_dump()["headers"] == {"Accept": "application/json"}


def test_model_copy_revalidates_header_updates() -> None:
    """model_copy(update=...) must not turn a safe record into a secret payload."""
    profile = ProviderProfileRecord(
        id="custom-provider",
        name="Custom Provider",
        protocol="openai_chat",
        base_url="https://provider.test",
    )

    with pytest.raises(ValueError, match="safe metadata allowlist"):
        profile.model_copy(update={"headers": {"X-Access-Token": "plaintext-token"}})

    copied = profile.model_copy(update={"headers": {"X-Title": "Workbench"}})

    assert isinstance(copied.headers, SafeHeaders)
    assert copied.model_dump_json().count("Workbench") == 1


def test_deep_model_copy_preserves_immutable_safe_headers() -> None:
    """Deep copies must keep the immutable mapping usable for persistence."""
    profile = ProviderProfileRecord(
        id="custom-provider",
        name="Custom Provider",
        protocol="openai_chat",
        base_url="https://provider.test",
        headers={"Accept": "application/json"},
    )

    copied = profile.model_copy(deep=True)

    assert copied.headers is profile.headers
    assert copied.model_dump()["headers"] == {"Accept": "application/json"}


def test_profile_assignment_revalidates_headers() -> None:
    """Replacing the controlled mapping also cannot introduce an unsafe header."""
    profile = ProviderProfileRecord(
        id="custom-provider",
        name="Custom Provider",
        protocol="openai_chat",
        base_url="https://provider.test",
    )

    with pytest.raises(ValidationError):
        profile.headers = {"Cookie": "session=plaintext"}


def test_profile_persists_enabled_state_and_deepseek_requires_thinking() -> None:
    disabled = ProviderProfileRecord(
        id="local",
        name="Local",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
        enabled=False,
    )

    assert disabled.model_dump()["enabled"] is False
    assert ProviderProfileRecord.deepseek(id="deepseek").enabled is True
    with pytest.raises(ValidationError, match="thinking"):
        ProviderProfileRecord.deepseek(id="deepseek", thinking_enabled=False)
