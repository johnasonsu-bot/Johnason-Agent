use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderRequest {
    pub provider_ref: String,
    pub model: String,
}

impl ProviderRequest {
    pub fn from_envelope(envelope: &Value) -> Result<Self, String> {
        reject_sensitive_fields(envelope)?;
        let object = envelope.as_object().ok_or("envelope must be an object")?;
        let provider_ref = object
            .get("provider_ref")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("provider_ref is required")?
            .to_owned();
        let model = object
            .get("model")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("model is required")?
            .to_owned();
        Ok(Self {
            provider_ref,
            model,
        })
    }

    pub fn is_fixture(&self) -> bool {
        self.provider_ref == "provider-profile:fixture"
    }
}

fn reject_sensitive_fields(value: &Value) -> Result<(), String> {
    match value {
        Value::Object(object) => {
            for (key, nested) in object {
                let normalized = key.to_ascii_lowercase().replace('-', "_");
                if matches!(
                    normalized.as_str(),
                    "api_key"
                        | "apikey"
                        | "authorization"
                        | "credential"
                        | "credentials"
                        | "password"
                        | "secret"
                        | "token"
                ) {
                    return Err("provider material is forbidden in Host v2 frames".into());
                }
                reject_sensitive_fields(nested)?;
            }
        }
        Value::Array(values) => {
            for nested in values {
                reject_sensitive_fields(nested)?;
            }
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::ProviderRequest;
    use serde_json::json;

    #[test]
    fn provider_bridge_accepts_only_an_opaque_reference() {
        let request = ProviderRequest::from_envelope(&json!({
            "provider_ref":"provider-profile:fixture","model":"fixture-model"
        }))
        .expect("opaque provider reference");
        assert_eq!(request.provider_ref, "provider-profile:fixture");
        assert!(
            ProviderRequest::from_envelope(&json!({
                "provider_ref":"provider-profile:fixture","model":"fixture-model",
                "api_key":"forbidden"
            }))
            .is_err()
        );
    }
}
