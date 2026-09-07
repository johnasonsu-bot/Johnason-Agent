mod event_mapper;
mod grant_channel;
mod native_agent;
mod protocol;
mod provider_bridge;
mod query;
mod tool_transport;

use std::env;
use std::io::{self, Write};
use std::sync::Arc;

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use event_mapper::map_event;
use native_agent::{NativeRunIdentity, PlatformToolManifest, stream_native_agent_to};
use protocol::{ControlFrame, response};
use provider_bridge::{ProviderRequest, ProviderStreamEvent};
use query::QueryMachine;
use tool_transport::ToolTransport;

struct ActiveTask {
    run_id: String,
    cancel: CancellationToken,
    task: JoinHandle<Result<(), String>>,
    transport: Arc<ToolTransport>,
    cancellation_requested: bool,
    terminal_allowed: bool,
    deferred_cancel_command_id: Option<String>,
}

impl ActiveTask {
    fn new(
        run_id: String,
        cancel: CancellationToken,
        task: JoinHandle<Result<(), String>>,
        transport: Arc<ToolTransport>,
    ) -> Self {
        Self {
            run_id,
            cancel,
            task,
            transport,
            cancellation_requested: false,
            terminal_allowed: true,
            deferred_cancel_command_id: None,
        }
    }

    fn signal_cancel(&mut self) {
        self.cancellation_requested = true;
        self.cancel.cancel();
    }

    fn request_query_cancel(&mut self, command_id: String) -> Result<(), String> {
        if self.deferred_cancel_command_id.is_some() {
            return Err("Goose query cancellation is already pending".into());
        }
        self.deferred_cancel_command_id = Some(command_id);
        self.signal_cancel();
        Ok(())
    }

    fn deferred_cancel_command_id(&self) -> Option<&str> {
        self.deferred_cancel_command_id.as_deref()
    }

    fn cancellation_requested(&self) -> bool {
        self.cancellation_requested
    }

    fn request_input_loss_shutdown(&mut self) {
        self.terminal_allowed = false;
        self.signal_cancel();
        self.transport.close(
            "Goose tool transport input closed before settlement; effect state is unknown",
        );
    }

    fn may_publish_terminal(&self) -> bool {
        self.terminal_allowed
    }

    #[cfg(test)]
    fn task_finished(&self) -> bool {
        self.task.is_finished()
    }

    #[cfg(test)]
    fn abort_for_test_cleanup(&self) {
        self.task.abort();
    }
}

const BUILD_ID: &str = if MODEL_HOST { "goose-host-v2:model-host-r1" } else { "goose-host-v2:fixture-wrapper-r2" };
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
    let mut active_task: Option<ActiveTask> = None;
    let mut active_events: Option<mpsc::UnboundedReceiver<ProviderStreamEvent>> = None;
    let mut active_tool_requests: Option<mpsc::UnboundedReceiver<Value>> = None;
    loop {
        if !input_open && active_task.is_none() {
            break;
        }
        tokio::select! {
            line = stdin.next_line(), if input_open => {
                let Some(line) = line.map_err(|_| "Host v2 input failed")? else {
                    input_open = false;
                    if let Some(active) = active_task.as_mut() {
                        active.request_input_loss_shutdown();
                    }
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
                            "query":true,"model":MODEL_HOST,"tools":false,"skills":false,
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
                if MODEL_HOST && provider.is_fixture() {
                    return Err("model Host rejects fixture provider".into());
                }
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
                    let native_identity = NativeRunIdentity::from_envelope(envelope)?;
                    PlatformToolManifest::from_envelope(envelope)?;
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
                            let native_envelope = envelope.clone();
                            let cancel = CancellationToken::new();
                            let task_cancel = cancel.clone();
                            let (tool_sender, tool_receiver) = mpsc::unbounded_channel();
                            let transport = Arc::new(ToolTransport::new(
                                native_identity,
                                tool_sender,
                            ));
                            let task_transport = transport.clone();
                            let task = tokio::spawn(async move {
                                stream_native_agent_to(
                                    material,
                                    prompt,
                                    native_envelope,
                                    Some(task_transport),
                                    task_cancel,
                                    sender,
                                )
                                .await
                            });
                            active_task = Some(ActiveTask::new(
                                task_run_id,
                                cancel,
                                task,
                                transport,
                            ));
                            active_events = Some(receiver);
                            active_tool_requests = Some(tool_receiver);
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
                if let Some(active) = active_task.as_mut() {
                    if active.run_id != run_id {
                        return Err("cancel run id does not match the active query".into());
                    }
                    active.request_query_cancel(command.command_id)?;
                    continue;
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
            "tool.result" => {
                let active = active_task
                    .as_ref()
                    .ok_or("Goose tool.result has no active query")?;
                active.transport.deliver_result(&json!({
                    "kind":"command",
                    "type":"tool.result",
                    "command_id":command.command_id,
                    "payload":command.payload,
                }))?;
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
            frame = async {
                active_tool_requests
                    .as_mut()
                    .expect("active tool request receiver")
                    .recv()
                    .await
            }, if active_tool_requests.is_some() => {
                if let Some(frame) = frame {
                    write_frame(&mut stdout, &frame)?;
                } else {
                    active_tool_requests = None;
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
                    active_tool_requests = None;
                    let active = active_task
                        .take()
                        .ok_or("Goose provider task is unavailable")?;
                    let identity = machine
                        .active_identity()
                        .cloned()
                        .ok_or("Goose provider completed without an active query")?;
                    let cancellation_requested = active.cancellation_requested();
                    let terminal_allowed = active.may_publish_terminal();
                    let cancel_command_id = active.deferred_cancel_command_id().map(str::to_owned);
                    let result = active.task
                        .await
                        .map_err(|_| "Goose provider task failed".to_owned())?;
                    if !terminal_allowed {
                        return Err(
                            "Goose Host input closed with an unsettled query; terminal is unknown"
                                .into(),
                        );
                    }
                    let events = if cancellation_requested {
                        write_frame(
                            &mut stdout,
                            &response(
                                "query.cancel",
                                cancel_command_id.as_deref().ok_or(
                                    "Goose cancelled query has no deferred cancel command",
                                )?,
                                json!({"accepted":true}),
                            ),
                        )?;
                        vec![machine.cancel(&identity.run_id)?]
                    } else {
                        machine.finish_real(result)?
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
    use std::sync::Arc;

    use tokio_util::sync::CancellationToken;

    use super::{ActiveTask, validate_argv, validate_process_environment};
    use crate::native_agent::NativeRunIdentity;
    use crate::tool_transport::ToolTransport;

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

    // Break caught: query.cancel must signal the running native Session without
    // aborting its task before a pending platform write can settle.
    #[tokio::test]
    async fn active_query_cancellation_keeps_the_native_task_alive_for_settlement() {
        let cancel = CancellationToken::new();
        let task_cancel = cancel.clone();
        let task = tokio::spawn(async move {
            task_cancel.cancelled().await;
            std::future::pending::<Result<(), String>>().await
        });
        let (outgoing, _receiver) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(
            NativeRunIdentity {
                session_id: "session-1".into(),
                run_id: "run-1".into(),
                term_id: "term-1".into(),
                step_id: "step-1".into(),
                command_id: "command-1".into(),
                agent_id: "agent-1".into(),
                agent_role: "worker".into(),
            },
            outgoing,
        ));
        let mut active = ActiveTask::new("run-1".into(), cancel, task, transport);

        active
            .request_query_cancel("cancel-command-1".into())
            .expect("request cancellation");
        tokio::task::yield_now().await;

        assert!(active.cancellation_requested());
        assert_eq!(
            active.deferred_cancel_command_id(),
            Some("cancel-command-1")
        );
        assert!(!active.task_finished());
        active.abort_for_test_cleanup();
    }

    // Break caught: treating stdin EOF like query.cancel could publish a false
    // cancelled/completed seal while an effectful tool outcome is still unknown.
    #[tokio::test]
    async fn input_loss_never_allows_a_query_terminal_to_be_published() {
        let cancel = CancellationToken::new();
        let task = tokio::spawn(std::future::pending::<Result<(), String>>());
        let (outgoing, _receiver) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(
            NativeRunIdentity {
                session_id: "session-1".into(),
                run_id: "run-1".into(),
                term_id: "term-1".into(),
                step_id: "step-1".into(),
                command_id: "command-1".into(),
                agent_id: "agent-1".into(),
                agent_role: "worker".into(),
            },
            outgoing,
        ));
        let mut active = ActiveTask::new("run-1".into(), cancel, task, transport);

        active.request_input_loss_shutdown();

        assert!(active.cancellation_requested());
        assert!(!active.may_publish_terminal());
        active.abort_for_test_cleanup();
    }
}
