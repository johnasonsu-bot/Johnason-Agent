mod event_mapper;
mod grant_channel;
mod protocol;
mod provider_bridge;
mod query;

use std::env;
use std::io::{self, Write};

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use event_mapper::map_event;
use protocol::{ControlFrame, response};
use provider_bridge::{ProviderRequest, ProviderStreamEvent, stream_provider_to};
use query::QueryMachine;

const BUILD_ID: &str = "goose-host-v2:fixture-wrapper-r2";
const ALLOWED_PROCESS_ENVIRONMENT_NAMES: &[&str] = &[
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PYTHONUTF8",
    "TMPDIR",
    "TZ",
    // macOS injects this locale descriptor while loading the system trust
    // provider used by Goose's rustls stack. It is not application config.
    "__CF_USER_TEXT_ENCODING",
];

fn validate_argv(arguments: &[String]) -> Result<(), String> {
    if arguments.len() != 1 {
        return Err("Goose Host v2 rejects argv configuration".into());
    }
    Ok(())
}

fn validate_process_environment<I>(environment: I) -> Result<(), String>
where
    I: IntoIterator<Item = (String, String)>,
{
    for (name, _) in environment {
        if !ALLOWED_PROCESS_ENVIRONMENT_NAMES.contains(&name.as_str()) {
            return Err(format!("unauthorized process environment: {name}"));
        }
    }
    Ok(())
}

fn write_frame(stdout: &mut impl Write, frame: &Value) -> Result<(), String> {
    serde_json::to_writer(&mut *stdout, frame).map_err(|_| "Host v2 output encoding failed")?;
    stdout
        .write_all(b"\n")
        .map_err(|_| "Host v2 output failed")?;
    stdout
        .flush()
        .map_err(|_| "Host v2 output flush failed".to_owned())
}

#[tokio::main]
async fn main() {
    if let Err(message) = run().await {
        eprintln!("GOOSE_HOST_V2_BLOCKED:{message}");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), String> {
    let arguments: Vec<String> = env::args().collect();
    validate_argv(&arguments)?;
    let descriptor = env::var(grant_channel::PROVIDER_GRANT_FD_ENV)
        .map_err(|_| "Goose Provider Grant descriptor is missing")?
        .parse::<i32>()
        .map_err(|_| "Goose Provider Grant descriptor is invalid")?;
    // SAFETY: no thread exists yet, so mutating this one internal variable
    // cannot race another environment reader.
    unsafe { env::remove_var(grant_channel::PROVIDER_GRANT_FD_ENV) };
    validate_process_environment(env::vars())?;
    let mut grant_receiver = grant_channel::GrantReceiver::start(descriptor)?;
    let mut stdin = BufReader::new(tokio::io::stdin()).lines();
    let mut stdout = io::stdout().lock();
    let mut machine = QueryMachine::default();
    let mut input_open = true;
    let mut active_task: Option<(String, JoinHandle<Result<(), String>>)> = None;
    let mut active_events: Option<mpsc::UnboundedReceiver<ProviderStreamEvent>> = None;
    loop {
        if !input_open && active_task.is_none() {
            break;
        }
        tokio::select! {
            line = stdin.next_line(), if input_open => {
                let Some(line) = line.map_err(|_| "Host v2 input failed")? else {
                    input_open = false;
                    continue;
                };
                let command = ControlFrame::parse(line.as_bytes())?;
                match command.command_type.as_str() {
            "runtime.capabilities" => {
                write_frame(
                    &mut stdout,
                    &response(
                        &command.command_type,
                        &command.command_id,
                        json!({
                            "runtime_id":"goose",
                            "build_id":BUILD_ID,
                            "protocol_version":"2.0",
                            "query":true,"model":false,"tools":false,"skills":false,
                            "plugins":false,"workspace":false,"interventions":false,
                            "pause_resume":false,"compaction":false,"checkpoints":false,
                            "streaming":true,"plan":false,"todo":false,
                            "prompt_sections":false,"tool_interceptors":false,
                            "event_cursor":true
                        }),
                    ),
                )?;
            }
            "query.start" => {
                let envelope = command
                    .payload
                    .get("envelope")
                    .ok_or("query.start envelope is missing")?;
                let runtime_input = command
                    .payload
                    .get("runtime_input")
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                let provider = ProviderRequest::from_envelope(envelope)?;
                let identity = envelope.as_object().ok_or("envelope must be an object")?;
                let run_id = identity
                    .get("run_id")
                    .and_then(Value::as_str)
                    .ok_or("run_id is missing")?;
                let term_id = identity
                    .get("term_id")
                    .and_then(Value::as_str)
                    .ok_or("term_id is missing")?;
                let step_id = identity
                    .get("step_id")
                    .and_then(Value::as_str)
                    .ok_or("step_id is missing")?;
                let private_grant = grant_receiver.receive()?;
                if provider.is_fixture() {
                    let fixture_disposition =
                    private_grant.fixture_disposition(
                        &command.command_id,
                        run_id,
                        term_id,
                        step_id,
                        &provider.provider_ref,
                        &provider.model,
                    )?;
                    match machine.start(envelope, &runtime_input, fixture_disposition) {
                        Ok(events) => {
                            write_frame(
                                &mut stdout,
                                &response(
                                    &command.command_type,
                                    &command.command_id,
                                    json!({"accepted":true}),
                                ),
                            )?;
                            for event in events {
                                write_frame(&mut stdout, &map_event(event, run_id, term_id, step_id))?;
                            }
                        }
                        Err(_) => {
                            write_frame(
                                &mut stdout,
                                &response(
                                    &command.command_type,
                                    &command.command_id,
                                    json!({"accepted":false}),
                                ),
                            )?;
                        }
                    }
                } else {
                    let material = private_grant.into_provider_material(
                        &command.command_id,
                        run_id,
                        term_id,
                        step_id,
                        &provider.provider_ref,
                    )?;
                    match machine.start_real(envelope, &runtime_input) {
                        Ok((running, prompt)) => {
                            write_frame(
                                &mut stdout,
                                &response(
                                    &command.command_type,
                                    &command.command_id,
                                    json!({"accepted":true}),
                                ),
                            )?;
                            write_frame(&mut stdout, &map_event(running, run_id, term_id, step_id))?;
                            let (sender, receiver) = mpsc::unbounded_channel();
                            let task_run_id = run_id.to_owned();
                            let task = tokio::spawn(async move {
                                stream_provider_to(material, prompt, sender).await
                            });
                            active_task = Some((task_run_id, task));
                            active_events = Some(receiver);
                        }
                        Err(_) => {
                            write_frame(
                                &mut stdout,
                                &response(
                                    &command.command_type,
                                    &command.command_id,
                                    json!({"accepted":false}),
                                ),
                            )?;
                        }
                    }
                }
            }
            "query.cancel" => {
                let run_id = command
                    .payload
                    .get("run_id")
                    .and_then(Value::as_str)
                    .ok_or("cancel run_id is missing")?;
                if active_task
                    .as_ref()
                    .is_some_and(|(active_run_id, _)| active_run_id == run_id)
                {
                    let (_, task) = active_task.take().expect("active task was checked");
                    task.abort();
                    active_events = None;
                }
                let event = machine.cancel(run_id)?;
                write_frame(
                    &mut stdout,
                    &response(
                        &command.command_type,
                        &command.command_id,
                        json!({"accepted":true}),
                    ),
                )?;
                let (identity, _, _) = machine.terminal().ok_or("cancel terminal is missing")?;
                write_frame(
                    &mut stdout,
                    &map_event(
                        event,
                        &identity.run_id,
                        &identity.term_id,
                        &identity.step_id,
                    ),
                )?;
            }
            "query.status" => {
                let (identity, cursor, _) =
                    machine.terminal().ok_or("terminal status is unavailable")?;
                let requested_cursor = command
                    .payload
                    .get("terminal_cursor")
                    .and_then(Value::as_u64);
                if command.payload.get("run_id").and_then(Value::as_str) != Some(&identity.run_id)
                    || command.payload.get("term_id").and_then(Value::as_str)
                        != Some(&identity.term_id)
                    || command.payload.get("step_id").and_then(Value::as_str)
                        != Some(&identity.step_id)
                    || requested_cursor != Some(cursor)
                {
                    return Err("terminal seal identity mismatch".into());
                }
                write_frame(
                    &mut stdout,
                    &response(
                        &command.command_type,
                        &command.command_id,
                        json!({
                            "state":"terminal","run_id":identity.run_id,
                            "term_id":identity.term_id,"step_id":identity.step_id,
                            "terminal_cursor":cursor,"sealed":true
                        }),
                    ),
                )?;
            }
            _ => {
                write_frame(
                    &mut stdout,
                    &response(
                        &command.command_type,
                        &command.command_id,
                        json!({"accepted":false}),
                    ),
                )?;
            }
                }
            }
            event = async {
                active_events
                    .as_mut()
                    .expect("active event receiver")
                    .recv()
                    .await
            }, if active_events.is_some() => {
                if let Some(event) = event {
                    let identity = machine
                        .active_identity()
                        .cloned()
                        .ok_or("Goose provider emitted an event without an active query")?;
                    if let Some(event) = machine.push_real(event)? {
                        write_frame(
                            &mut stdout,
                            &map_event(event, &identity.run_id, &identity.term_id, &identity.step_id),
                        )?;
                    }
                } else {
                    active_events = None;
                    let (_, task) = active_task
                        .take()
                        .ok_or("Goose provider task is unavailable")?;
                    let identity = machine
                        .active_identity()
                        .cloned()
                        .ok_or("Goose provider completed without an active query")?;
                    let result = task
                        .await
                        .map_err(|_| "Goose provider task failed".to_owned())?;
                    let events = match result {
                        Ok(()) => match machine.complete_real() {
                            Ok(events) => events,
                            Err(_) => vec![machine.fail_real("empty_output")?],
                        },
                        Err(_) => vec![machine.fail_real("provider_failed")?],
                    };
                    for event in events {
                        write_frame(
                            &mut stdout,
                            &map_event(event, &identity.run_id, &identity.term_id, &identity.step_id),
                        )?;
                    }
                }
            }
        }
        if !input_open && active_task.is_none() {
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{validate_argv, validate_process_environment};

    #[test]
    fn argv_cannot_carry_provider_material() {
        assert!(validate_argv(&["goose-host-v2".into()]).is_ok());
        assert!(validate_argv(&["goose-host-v2".into(), "--provider-key".into()]).is_err());
    }

    #[test]
    fn child_environment_is_closed_but_retains_process_basics() {
        let safe = vec![
            ("PATH".to_owned(), "/usr/bin".to_owned()),
            ("HOME".to_owned(), "/tmp/home".to_owned()),
            ("TMPDIR".to_owned(), "/tmp".to_owned()),
            ("LANG".to_owned(), "C.UTF-8".to_owned()),
            (
                "__CF_USER_TEXT_ENCODING".to_owned(),
                "0x1F5:0x0:0x0".to_owned(),
            ),
        ];
        assert!(validate_process_environment(safe).is_ok());

        for name in [
            "AWS_SECRET_ACCESS_KEY",
            "AZURE_OPENAI_API_KEY",
            "OPENAI_API_TOKEN",
            "MY_PROVIDER_CREDENTIAL",
            "UNDECLARED_HARMLESS_VALUE",
        ] {
            assert!(validate_process_environment(vec![(name.into(), "secret".into())]).is_err());
        }
    }
}
