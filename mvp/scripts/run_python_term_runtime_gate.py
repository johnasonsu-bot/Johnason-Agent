#!/usr/bin/env python3
"""Run the deterministic Python Term gate on one fixed source revision."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from workbench.models.contracts import ModelMessage, ModelRequest
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.profiles import ProviderCapability, ProviderProfileRecord
from workbench.runtime.engine_host.v2.contracts import RuntimeCapabilitiesV2
from workbench.runtime.python_term.gate import (
    REQUIRED_GATE_SCENARIOS,
    PythonTermGateScenario,
    PythonTermGateTrust,
    build_python_term_gate_verdict,
    load_signed_python_term_gate_verdict,
    python_term_gate_signing_document,
    python_term_gate_source_revision,
)
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID
from workbench.runtime.python_term.sdk_adapter import PINNED_AGENTS_SDK_REVISION


SCENARIO_COMMANDS: dict[str, tuple[str, ...]] = {
    "sdk_provenance": (
        "tests/acceptance/test_python_sdk_provenance.py",
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_calls_the_pinned_agents_sdk_runner_not_a_contract_fake",
        "tests/acceptance/test_python_term_runtime_gate.py::test_control_plane_sdk_model_calls_the_existing_gateway_authority",
    ),
    "frozen_identity": (
        "tests/unit/runtime/python_term/test_contracts.py",
        "tests/acceptance/test_python_term_compatibility.py::test_python_term_rejects_unresolved_authorities_before_pin_or_turn",
    ),
    "private_context_and_step_isolation": (
        "tests/integration/test_python_term_runtime.py",
    ),
    "tool_workspace_pty_policy": (
        "tests/unit/runtime/python_term/test_tool_router.py",
        "tests/integration/test_python_term_pty_isolation.py",
    ),
    "effect_exactly_once_and_reconciliation": (
        "tests/integration/test_python_term_tool_effects.py",
    ),
    "cursor_checkpoint_restart_projection": (
        "tests/integration/test_python_term_recovery.py",
    ),
    "host_v1_flag_and_no_fallback": (
        "tests/integration/test_python_term_routing.py",
        "tests/acceptance/test_python_term_compatibility.py",
        "tests/acceptance/test_python_term_runtime_gate.py::test_control_plane_worker_executes_a_durable_python_term_without_v1_fallback",
    ),
    "session_lock_ownership": (
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_observable_lock_asserts_real_ownership_at_admission",
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_lock_bypass_mutation_fails_at_the_protected_admission_entry",
        "tests/acceptance/test_python_term_runtime_gate.py::test_normal_admission_holds_the_observable_real_lock_at_entry",
        "tests/acceptance/test_python_term_compatibility.py::test_same_session_v1_and_python_term_admission_serializes_at_lock_boundary",
    ),
    "proof_binding": (
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_proof_binds_source_runtime_capabilities_and_complete_results",
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_cannot_issue_go_for_missing_or_failed_deterministic_scenario",
        "tests/acceptance/test_python_term_runtime_gate.py::test_gate_rejects_live_evidence_inside_the_deterministic_proof_matrix",
        "tests/acceptance/test_python_term_runtime_gate.py::test_production_composition_fails_closed_without_signed_build_proof",
    ),
}


def _capabilities() -> RuntimeCapabilitiesV2:
    return RuntimeCapabilitiesV2(
        runtime_id="python-term",
        build_id=RUNTIME_BUILD_ID,
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )


def _run_scenario(scenario_id: str) -> tuple[PythonTermGateScenario, dict[str, object]]:
    targets = SCENARIO_COMMANDS[scenario_id]
    command = [sys.executable, "-m", "pytest", "-q", *targets]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    status = "PASS" if completed.returncode == 0 else "FAIL"
    summary = completed.stdout.strip().splitlines()[-1:] or ["no pytest summary"]
    scenario = PythonTermGateScenario(
        scenario_id=scenario_id,
        status=status,
        command_summary="pytest:" + scenario_id,
    )
    return scenario, {
        "scenario_id": scenario_id,
        "status": status,
        "command": command,
        "summary": summary[0][:512],
    }


async def _live_lmstudio_smoke() -> dict[str, object]:
    profile = ProviderProfileRecord(
        id="gate-lmstudio",
        name="Gate LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
        model_aliases={"default": "local-model"},
        capabilities={ProviderCapability.STREAMING},
    )
    gateway = ModelGateway({"lmstudio": LMStudioProvider(profile.base_url)})
    try:
        models = await asyncio.wait_for(gateway.list_models(profile), timeout=2)
        if not models:
            return {"status": "LIVE_PROVIDER_NOT_EVALUATED"}
        response = await asyncio.wait_for(
            gateway.complete(
                ModelRequest(
                    model=models[0],
                    messages=[ModelMessage(role="user", content="Reply READY")],
                ),
                profile.model_copy(update={"model_aliases": {"default": models[0]}}),
            ),
            timeout=20,
        )
        return {
            "status": "PASS" if isinstance(response.text, str) else "FAIL",
            "provider": "lmstudio",
        }
    except Exception:
        return {"status": "LIVE_PROVIDER_NOT_EVALUATED"}
    finally:
        await gateway.aclose()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--development-runtime-dir", type=Path)
    parser.add_argument("--development-public-key", type=Path)
    return parser.parse_args()


def _selected_trust(arguments: argparse.Namespace) -> PythonTermGateTrust:
    development = (
        arguments.development_runtime_dir,
        arguments.development_public_key,
        arguments.proof,
    )
    if any(value is not None for value in development):
        if not all(value is not None for value in development):
            raise ValueError("development trust requires runtime, public key and proof")
        return PythonTermGateTrust.development(
            runtime_dir=arguments.development_runtime_dir,
            public_key_path=arguments.development_public_key,
            proof_path=arguments.proof,
        )
    if arguments.proof is not None:
        raise ValueError("production proof path cannot be overridden")
    return PythonTermGateTrust.production()


def _verify_current_build(arguments: argparse.Namespace) -> tuple[dict[str, object], int]:
    capabilities = _capabilities()
    trust_status = (
        "DEV_UNTRUSTED"
        if arguments.development_runtime_dir is not None
        else "PRODUCTION_TRUSTED"
    )
    try:
        trust = _selected_trust(arguments)
        trust_status = trust.trust_status
        verdict = load_signed_python_term_gate_verdict(capabilities, trust=trust)
    except (OSError, RuntimeError, TypeError, ValueError):
        return {
            "source_revision": python_term_gate_source_revision(),
            "Decision": "BLOCKED",
            "trust_status": trust_status,
        }, 1
    return {
        "source_revision": verdict.source_revision,
        "result_digest": verdict.result_digest,
        "key_id": trust.key_id,
        "Decision": verdict.decision,
        "trust_status": trust.trust_status,
    }, 0


def main() -> int:
    arguments = _arguments()
    if arguments.verify_only:
        document, code = _verify_current_build(arguments)
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return code
    source_revision = python_term_gate_source_revision()
    scenarios: list[PythonTermGateScenario] = []
    evidence: list[dict[str, object]] = []
    for scenario_id in REQUIRED_GATE_SCENARIOS:
        scenario, record = _run_scenario(scenario_id)
        scenarios.append(scenario)
        evidence.append(record)
    capabilities = _capabilities()
    try:
        verdict = build_python_term_gate_verdict(
            source_revision=source_revision,
            capabilities=capabilities,
            scenarios=tuple(scenarios),
        )
    except ValueError:
        decision = "BLOCKED"
        signing_payload = None
        result_digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    else:
        result_digest = verdict.result_digest
        signing_payload = python_term_gate_signing_document(verdict)
        try:
            trust = _selected_trust(arguments)
            trust_status = trust.trust_status
            packaged = load_signed_python_term_gate_verdict(capabilities, trust=trust)
        except (OSError, RuntimeError, TypeError, ValueError):
            decision = "BLOCKED_EXTERNAL_SIGNATURE_REQUIRED"
            trust_status = (
                "DEV_UNTRUSTED"
                if arguments.development_runtime_dir is not None
                else "PRODUCTION_TRUSTED"
            )
        else:
            decision = (
                packaged.decision
                if packaged == verdict
                else "BLOCKED_EXTERNAL_SIGNATURE_REQUIRED"
            )
            trust_status = trust.trust_status
    document = {
        "source_revision": source_revision,
        "sdk_revision": PINNED_AGENTS_SDK_REVISION,
        "runtime_id": capabilities.runtime_id,
        "build_id": capabilities.build_id,
        "protocol_version": capabilities.protocol_version,
        "scenarios": evidence,
        "live_smoke": asyncio.run(_live_lmstudio_smoke()),
        "result_digest": result_digest,
        "external_signature_payload": signing_payload,
        "Decision": decision,
        "trust_status": trust_status if signing_payload is not None else "PRODUCTION_TRUSTED",
        "Goose runtime status": "NOT_YET_EVALUATED",
        "DSH runtime status": "NOT_YET_EVALUATED",
    }
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if decision == "GO_PYTHON_TERM_RUNTIME" else 1


if __name__ == "__main__":
    raise SystemExit(main())
