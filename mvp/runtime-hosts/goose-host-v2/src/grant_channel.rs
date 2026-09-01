use std::fs::File;
use std::io::Read;
use std::os::fd::{FromRawFd, RawFd};

use serde::Deserialize;

const MAX_GRANT_BYTES: u64 = 65_536;

pub fn validate_preopened_fd(fd: RawFd) -> Result<(), String> {
    if fd <= 2 {
        return Err("provider Grant Channel must use a private pre-opened descriptor".into());
    }
    Ok(())
}

pub struct GrantMaterial {
    bytes: Vec<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixtureDisposition {
    Complete,
    Fail,
    Hold,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureBinding {
    schema: String,
    command_id: String,
    run_id: String,
    term_id: String,
    step_id: String,
    provider_ref: String,
    model: String,
    outcome: FixtureDisposition,
}

impl GrantMaterial {
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    #[allow(clippy::too_many_arguments)]
    pub fn fixture_disposition(
        &self,
        command_id: &str,
        run_id: &str,
        term_id: &str,
        step_id: &str,
        provider_ref: &str,
        model: &str,
    ) -> Result<FixtureDisposition, String> {
        parse_fixture_binding(
            &self.bytes,
            command_id,
            run_id,
            term_id,
            step_id,
            provider_ref,
            model,
        )
    }
}

impl Drop for GrantMaterial {
    fn drop(&mut self) {
        self.bytes.fill(0);
    }
}

pub fn read_once(fd: RawFd) -> Result<GrantMaterial, String> {
    validate_preopened_fd(fd)?;
    // SAFETY: the Supervisor transfers ownership of this pre-opened descriptor
    // to the sidecar. It is consumed once and closed when `file` is dropped.
    let file = unsafe { File::from_raw_fd(fd) };
    let mut bytes = Vec::new();
    file.take(MAX_GRANT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "provider Grant Channel read failed")?;
    if bytes.is_empty() || bytes.len() as u64 > MAX_GRANT_BYTES {
        bytes.fill(0);
        return Err("provider Grant Channel payload is invalid".into());
    }
    Ok(GrantMaterial { bytes })
}

#[allow(clippy::too_many_arguments)]
fn parse_fixture_binding(
    bytes: &[u8],
    command_id: &str,
    run_id: &str,
    term_id: &str,
    step_id: &str,
    provider_ref: &str,
    model: &str,
) -> Result<FixtureDisposition, String> {
    let binding: FixtureBinding =
        serde_json::from_slice(bytes).map_err(|_| "fixture binding record is invalid")?;
    if binding.schema != "goose.fixture.binding.v1"
        || binding.command_id != command_id
        || binding.run_id != run_id
        || binding.term_id != term_id
        || binding.step_id != step_id
        || binding.provider_ref != provider_ref
        || binding.model != model
    {
        return Err("fixture binding identity mismatch".into());
    }
    Ok(binding.outcome)
}

#[cfg(test)]
mod tests {
    use super::{FixtureDisposition, parse_fixture_binding, validate_preopened_fd};
    use serde_json::json;

    #[test]
    fn grant_channel_must_be_private_and_preopened() {
        assert!(validate_preopened_fd(3).is_ok());
        assert!(validate_preopened_fd(0).is_err());
        assert!(validate_preopened_fd(1).is_err());
        assert!(validate_preopened_fd(2).is_err());
    }

    #[test]
    fn fixture_binding_is_secret_free_and_identity_bound() {
        let binding = json!({
            "schema":"goose.fixture.binding.v1",
            "command_id":"start-1",
            "run_id":"run-1",
            "term_id":"term-1",
            "step_id":"step-1",
            "provider_ref":"provider-profile:fixture",
            "model":"fixture-model",
            "outcome":"hold"
        });
        assert_eq!(
            parse_fixture_binding(
                binding.to_string().as_bytes(),
                "start-1",
                "run-1",
                "term-1",
                "step-1",
                "provider-profile:fixture",
                "fixture-model"
            )
            .expect("binding"),
            FixtureDisposition::Hold
        );
        assert!(
            parse_fixture_binding(
                binding.to_string().as_bytes(),
                "other-command",
                "run-1",
                "term-1",
                "step-1",
                "provider-profile:fixture",
                "fixture-model"
            )
            .is_err()
        );
    }
}
