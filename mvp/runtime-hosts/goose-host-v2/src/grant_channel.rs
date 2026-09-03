use std::collections::BTreeSet;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{FromRawFd, RawFd};
use std::thread::{self, JoinHandle};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const GRANT_MAGIC: &[u8; 8] = b"JAGTGRN1";
const ACK_MAGIC: &[u8; 8] = b"JAGTACK1";
const WIRE_VERSION: u8 = 1;
const MAX_HEADER_BYTES: usize = 65_536;
const MAX_SECRET_BYTES: usize = 65_536;
const MAX_ACK_BYTES: usize = 8_192;

pub const PROVIDER_GRANT_FD_ENV: &str = "WORKBENCH_PROVIDER_GRANT_FD";

pub fn validate_preopened_fd(fd: RawFd) -> Result<(), String> {
    if fd <= 2 {
        return Err("provider Grant Channel must use a private pre-opened descriptor".into());
    }
    Ok(())
}

fn restore_close_on_exec(fd: RawFd) -> Result<(), String> {
    validate_preopened_fd(fd)?;
    // SAFETY: fcntl only observes and updates flags on the validated descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err("provider Grant Channel descriptor is unavailable".into());
    }
    // SAFETY: the descriptor remains owned by this process until read_once.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err("provider Grant Channel containment failed".into());
    }
    Ok(())
}

pub struct GrantReceiver {
    task: Option<JoinHandle<Result<GrantMaterial, String>>>,
}

impl GrantReceiver {
    pub fn start(fd: RawFd) -> Result<Self, String> {
        restore_close_on_exec(fd)?;
        Ok(Self {
            task: Some(thread::spawn(move || read_once(fd))),
        })
    }

    pub fn receive(&mut self) -> Result<GrantMaterial, String> {
        let task = self
            .task
            .take()
            .ok_or("provider Grant Channel was already consumed")?;
        task.join()
            .map_err(|_| "provider Grant Channel receiver failed".to_owned())?
    }
}

pub struct GrantMaterial {
    binding: ProviderGrantBinding,
    bytes: Vec<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixtureDisposition {
    Complete,
    Fail,
    Hold,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderGrantTarget {
    runtime_id: String,
    build_id: String,
    lease_id: String,
    instance_id_digest: String,
    instance_nonce_digest: String,
    host_generation: String,
    lease_generation_seq: u64,
    expires_at: f64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderGrantBinding {
    grant_id: String,
    target: ProviderGrantTarget,
    session_id: String,
    command_id: String,
    run_id: String,
    term_id: String,
    step_id: String,
    provider_id: String,
    provider_profile_digest: String,
    model: String,
    scopes: Vec<String>,
    issued_at: f64,
    expires_at: f64,
    grant_nonce_digest: String,
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
        _model_alias: &str,
    ) -> Result<FixtureDisposition, String> {
        let expected_provider_ref = format!("provider-profile:{}", self.binding.provider_id);
        if self.binding.command_id != command_id
            || self.binding.run_id != run_id
            || self.binding.term_id != term_id
            || self.binding.step_id != step_id
            || expected_provider_ref != provider_ref
            || self.binding.model.is_empty()
        {
            return Err("provider Grant binding identity mismatch".into());
        }
        match self.binding.provider_id.as_str() {
            "fixture" | "fixture-completed" => Ok(FixtureDisposition::Complete),
            "fixture-failed" => Ok(FixtureDisposition::Fail),
            "fixture-held" => Ok(FixtureDisposition::Hold),
            _ => Err("fixed Goose provider is unsupported".into()),
        }
    }
}

impl Drop for GrantMaterial {
    fn drop(&mut self) {
        self.bytes.fill(0);
    }
}

fn read_once(fd: RawFd) -> Result<GrantMaterial, String> {
    validate_preopened_fd(fd)?;
    // SAFETY: the Supervisor transfers ownership of this pre-opened descriptor
    // to the sidecar. It is consumed once and closed when `file` is dropped.
    let mut file = unsafe { File::from_raw_fd(fd) };
    let mut prefix = [0_u8; 17];
    file.read_exact(&mut prefix)
        .map_err(|_| "provider Grant Channel framing is incomplete")?;
    if &prefix[..8] != GRANT_MAGIC || prefix[8] != WIRE_VERSION {
        return Err("provider Grant Channel framing is invalid".into());
    }
    let header_size = u32::from_be_bytes(prefix[9..13].try_into().expect("header size")) as usize;
    let secret_size = u32::from_be_bytes(prefix[13..17].try_into().expect("secret size")) as usize;
    if header_size == 0
        || header_size > MAX_HEADER_BYTES
        || secret_size == 0
        || secret_size > MAX_SECRET_BYTES
    {
        return Err("provider Grant Channel framing is invalid".into());
    }
    let mut header_bytes = vec![0_u8; header_size];
    file.read_exact(&mut header_bytes)
        .map_err(|_| "provider Grant Channel header is incomplete")?;
    let (binding, grant_digest) = parse_header(&header_bytes)?;
    header_bytes.fill(0);
    let mut secret = vec![0_u8; secret_size];
    if file.read_exact(&mut secret).is_err() {
        secret.fill(0);
        return Err("provider Grant Channel secret is incomplete".into());
    }
    let acknowledgement = json!({
        "schema":"workbench.runtime.provider_grant_ack.v1",
        "grant_id":binding.grant_id,
        "grant_digest":grant_digest,
        "target_instance_digest":binding.target.instance_id_digest,
    });
    let ack = serde_json::to_vec(&acknowledgement)
        .map_err(|_| "provider Grant Channel acknowledgement failed")?;
    if ack.is_empty() || ack.len() > MAX_ACK_BYTES {
        secret.fill(0);
        return Err("provider Grant Channel acknowledgement failed".into());
    }
    let mut ack_prefix = Vec::with_capacity(13);
    ack_prefix.extend_from_slice(ACK_MAGIC);
    ack_prefix.push(WIRE_VERSION);
    ack_prefix.extend_from_slice(&(ack.len() as u32).to_be_bytes());
    if file
        .write_all(&ack_prefix)
        .and_then(|_| file.write_all(&ack))
        .is_err()
    {
        secret.fill(0);
        return Err("provider Grant Channel acknowledgement failed".into());
    }
    file.flush()
        .map_err(|_| "provider Grant Channel acknowledgement failed")?;
    Ok(GrantMaterial {
        binding,
        bytes: secret,
    })
}

fn parse_header(bytes: &[u8]) -> Result<(ProviderGrantBinding, String), String> {
    let document: Value =
        serde_json::from_slice(bytes).map_err(|_| "provider Grant Channel header is invalid")?;
    let object = document
        .as_object()
        .ok_or("provider Grant Channel header is invalid")?;
    if object.len() != 3
        || object.get("schema").and_then(Value::as_str)
            != Some("workbench.runtime.provider_grant_private.v1")
    {
        return Err("provider Grant Channel header is invalid".into());
    }
    let grant_digest = object
        .get("grant_digest")
        .and_then(Value::as_str)
        .filter(|value| is_digest(value))
        .ok_or("provider Grant Channel header is invalid")?
        .to_owned();
    let binding_value = object
        .get("binding")
        .cloned()
        .ok_or("provider Grant Channel header is invalid")?;
    if canonical_value_digest(binding_value.clone())? != grant_digest {
        return Err("provider Grant Channel header is invalid".into());
    }
    let binding: ProviderGrantBinding = serde_json::from_value(binding_value)
        .map_err(|_| "provider Grant Channel binding is invalid")?;
    validate_binding(&binding)?;
    Ok((binding, grant_digest))
}

fn validate_binding(binding: &ProviderGrantBinding) -> Result<(), String> {
    let target = &binding.target;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "provider Grant Channel clock is invalid")?
        .as_secs_f64();
    let scopes: BTreeSet<&str> = binding.scopes.iter().map(String::as_str).collect();
    if target.runtime_id != "goose"
        || target.build_id != "goose-host-v2:fixture-wrapper-r2"
        || target.lease_generation_seq == 0
        || !is_digest(&target.instance_id_digest)
        || !is_digest(&target.instance_nonce_digest)
        || !is_digest(&binding.provider_profile_digest)
        || !is_digest(&binding.grant_nonce_digest)
        || binding.scopes.is_empty()
        || scopes.len() != binding.scopes.len()
        || binding.expires_at <= now
        || binding.expires_at > target.expires_at
        || binding.issued_at <= 0.0
        || binding.expires_at <= binding.issued_at
        || [
            &binding.grant_id,
            &binding.session_id,
            &binding.command_id,
            &binding.run_id,
            &binding.term_id,
            &binding.step_id,
            &binding.provider_id,
            &binding.model,
            &target.lease_id,
            &target.host_generation,
        ]
        .iter()
        .any(|value| value.is_empty())
        || binding.provider_id.contains([':', '/'])
    {
        return Err("provider Grant Channel binding is invalid or expired".into());
    }
    Ok(())
}

fn canonical_value_digest(value: Value) -> Result<String, String> {
    let encoded = serde_json::to_vec(&canonical(value))
        .map_err(|_| "provider Grant Channel binding is invalid")?;
    Ok(format!("{:x}", Sha256::digest(encoded)))
}

fn canonical(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(canonical).collect()),
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, canonical(value)))
                .collect(),
        ),
        scalar => scalar,
    }
}

fn is_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::validate_preopened_fd;

    #[test]
    fn grant_channel_must_be_private_and_preopened() {
        assert!(validate_preopened_fd(3).is_ok());
        assert!(validate_preopened_fd(0).is_err());
        assert!(validate_preopened_fd(1).is_err());
        assert!(validate_preopened_fd(2).is_err());
    }
}
