from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from tests.conformance.host_v2 import (
    FixedSmokeVerdict,
    assert_fixed_smoke_transcript,
)
from tests.fixtures.host_v2 import fake_v2_command, run_envelope
from tests.fixtures.runtime_query_v2 import canonical_runtime_query_input_v2
from workbench.runtime.deepseek_harness.source_gate import DeepSeekSourceVerifier
from workbench.runtime.engine_host.v2.contracts import (
    HostQueryCommandV2,
    RuntimeQueryInputV2,
)
from workbench.runtime.goose.source_gate import (
    build_and_attest_goose_query_smoke,
    verify_goose_query_smoke_readiness,
)


MVP_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = MVP_ROOT.parent
TEST_SECRET_VALUE = "fixed-smoke-private-value-97c1"
VAULT_SECRET_ID = "vault-secret-id-never-exported-97c1"
PRIVATE_SECRET_BASE64 = base64.b64encode(TEST_SECRET_VALUE.encode()).decode()
_SAFE_ENVIRONMENT = {
    name: os.environ[name]
    for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR", "TZ")
    if name in os.environ
}


def _command(
    runtime_id: str,
    *,
    command_id: str,
    provider_ref: str,
    runtime_input: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    materialized = RuntimeQueryInputV2.model_validate(runtime_input)
    envelope = run_envelope(
        runtime_id=runtime_id,
        command_id=command_id,
        overrides={
            "provider_ref": provider_ref,
            "model": "fixture-model",
            "message_snapshot_digest": materialized.message_snapshot_digest,
            "context.snapshot_digest": materialized.context_snapshot_digest,
            "prompt_manifest_digest": materialized.prompt_manifest_digest,
        },
    )
    command = HostQueryCommandV2.model_validate(
        {
            "type": "query.start",
            "command_id": command_id,
            "payload": {
                "envelope": envelope.model_dump(mode="json"),
                "runtime_input": runtime_input,
            },
        }
    ).model_dump(mode="json")
    return command, envelope.model_dump(mode="json")


def _wire_commands(
    start: dict[str, object],
    envelope: dict[str, object],
    *,
    terminal_cursor: int,
) -> list[dict[str, object]]:
    return [
        {
            "kind": "command",
            "type": "runtime.capabilities",
            "command_id": "cap-" + str(envelope["runtime"]["runtime_id"]),
            "payload": {},
        },
        {"kind": "command", **start},
        {
            "kind": "command",
            "type": "query.status",
            "command_id": "seal-" + str(envelope["runtime"]["runtime_id"]),
            "payload": {
                "run_id": envelope["run_id"],
                "term_id": envelope["term_id"],
                "step_id": envelope["step_id"],
                "terminal_cursor": terminal_cursor,
            },
        },
    ]


def _run_process(
    argv: tuple[str, ...],
    commands: list[dict[str, object]],
    *,
    private_record: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    stdin = "".join(
        json.dumps(command, sort_keys=True, separators=(",", ":")) + "\n"
        for command in commands
    )
    launched_argv = argv
    read_fd: int | None = None
    if private_record is not None:
        read_fd, write_fd = os.pipe()
        os.write(
            write_fd,
            (
                json.dumps(private_record, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode(),
        )
        os.close(write_fd)
        launcher = (
            "import os,sys;source=int(sys.argv[1]);os.dup2(source,3);"
            "os.set_inheritable(3,True);"
            "os.execve(sys.argv[2],sys.argv[2:],dict(os.environ))"
        )
        launched_argv = (sys.executable, "-c", launcher, str(read_fd), *argv)
    try:
        completed = subprocess.run(
            launched_argv,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=_SAFE_ENVIRONMENT,
            pass_fds=() if read_fd is None else (read_fd,),
        )
    finally:
        if read_fd is not None:
            os.close(read_fd)
    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    public_surfaces = {
        "host_transcript": {"commands": commands, "frames": frames},
        "environment": _SAFE_ENVIRONMENT,
        "argv": argv,
        "diagnostics": completed.stderr,
    }
    return frames, public_surfaces


def _python_projection_lane(runtime_input: dict[str, object]) -> FixedSmokeVerdict:
    projected = RuntimeQueryInputV2.model_validate(runtime_input)
    assert projected.model_dump(mode="json") == runtime_input
    start, envelope = _command(
        "fake-v2",
        command_id="start-python-projection",
        provider_ref="provider-profile:fixture",
        runtime_input=runtime_input,
    )
    commands = _wire_commands(start, envelope, terminal_cursor=4)
    frames, surfaces = _run_process(
        fake_v2_command("require_runtime_input"), commands
    )
    return assert_fixed_smoke_transcript(
        lane="python_projection",
        frames=frames,
        runtime_input=runtime_input,
        public_surfaces=surfaces,
        forbidden_values=(TEST_SECRET_VALUE, PRIVATE_SECRET_BASE64, VAULT_SECRET_ID),
    )


def _goose_release_lane(runtime_input: dict[str, object]) -> FixedSmokeVerdict:
    build_and_attest_goose_query_smoke(REPOSITORY_ROOT)
    assert verify_goose_query_smoke_readiness(REPOSITORY_ROOT).status == (
        "GO_GOOSE_QUERY_SMOKE"
    )
    start, envelope = _command(
        "goose",
        command_id="start-goose-convergence",
        provider_ref="provider-profile:fixture",
        runtime_input=runtime_input,
    )
    commands = _wire_commands(start, envelope, terminal_cursor=4)
    private_record = {
        "schema": "goose.fixture.binding.v1",
        "command_id": envelope["command_id"],
        "run_id": envelope["run_id"],
        "term_id": envelope["term_id"],
        "step_id": envelope["step_id"],
        "provider_ref": envelope["provider_ref"],
        "model": envelope["model"],
        "outcome": "complete",
    }
    binary = MVP_ROOT / "runtime-hosts/goose-host-v2/target/release/goose-host-v2"
    frames, surfaces = _run_process(
        (str(binary),), commands, private_record=private_record
    )
    return assert_fixed_smoke_transcript(
        lane="goose_release_smoke",
        frames=frames,
        runtime_input=runtime_input,
        public_surfaces=surfaces,
        forbidden_values=(TEST_SECRET_VALUE, PRIVATE_SECRET_BASE64, VAULT_SECRET_ID),
    )


def _dsh_built_lane(runtime_input: dict[str, object]) -> FixedSmokeVerdict:
    manifest = MVP_ROOT / "src/workbench/runtime/deepseek_harness/source_manifest.json"
    verdict = DeepSeekSourceVerifier().verify_plugin_smoke(REPOSITORY_ROOT, manifest)
    assert verdict["scope"] == "fixed_host_v2_sidecar_smoke"
    start, envelope = _command(
        "dsh",
        command_id="start-dsh-convergence",
        provider_ref="provider-profile:fixture-completed",
        runtime_input=runtime_input,
    )
    commands = _wire_commands(start, envelope, terminal_cursor=3)
    private_record = {
        "schema": "workbench.runtime.provider_grant_private.v1",
        "binding": {
            "grant_id": "grant-dsh-convergence",
            "grant_digest": "a" * 64,
            "target_instance_digest": "b" * 64,
            "command_id": envelope["command_id"],
            "run_id": envelope["run_id"],
            "term_id": envelope["term_id"],
            "step_id": envelope["step_id"],
            "provider_ref": envelope["provider_ref"],
            "model": envelope["model"],
            "expires_at": time.time() + 60,
        },
        "secret_base64": PRIVATE_SECRET_BASE64,
    }
    node = shutil.which("node")
    assert node is not None
    frames, surfaces = _run_process(
        (
            node,
            str(
                MVP_ROOT
                / "sidecars/deepseek-harness/dist/deepseek-harness-host-v2.mjs"
            ),
        ),
        commands,
        private_record=private_record,
    )
    return assert_fixed_smoke_transcript(
        lane="dsh_built_smoke",
        frames=frames,
        runtime_input=runtime_input,
        public_surfaces=surfaces,
        forbidden_values=(TEST_SECRET_VALUE, PRIVATE_SECRET_BASE64, VAULT_SECRET_ID),
    )


@pytest.mark.parametrize(
    ("expected_lane", "runner"),
    (
        ("python_projection", _python_projection_lane),
        ("goose_release_smoke", _goose_release_lane),
        ("dsh_built_smoke", _dsh_built_lane),
    ),
    ids=("python-projection", "goose-release", "dsh-built"),
)
def test_each_fixed_smoke_lane_reports_an_independent_non_federation_verdict(
    expected_lane,
    runner,
) -> None:
    runtime_input = canonical_runtime_query_input_v2().model_dump(mode="json")
    expected_digest = hashlib.sha256(
        json.dumps(
            runtime_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    verdict = runner(runtime_input)

    assert verdict.lane == expected_lane
    assert verdict.scope == "fixed_smoke_convergence"
    assert verdict.runtime_input_digest == expected_digest
    assert verdict.terminal_status == "completed"
    assert verdict.sealed is True
    assert "GO_RUNTIME_FEDERATION" not in json.dumps(
        verdict.__dict__, sort_keys=True
    )
