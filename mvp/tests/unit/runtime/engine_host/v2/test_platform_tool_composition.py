from copy import deepcopy
from pathlib import Path

import pytest

from tests.unit.runtime.engine_host.v2.test_runtime_admission import (
    _admission_system, _conversation_admission,
)
from workbench.main import RuntimeQueryRouter
from workbench.runtime.engine_host.v2.artifact_tools import ARTIFACT_TOOL_MANIFEST
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2
from workbench.runtime.engine_host.v2.platform_tool_context import PlatformToolContextFactory
from workbench.runtime.engine_host.v2.runtime_admission import RuntimeAdmissionConflict


def test_route_freezes_context_before_executor_and_reuses_it(tmp_path: Path) -> None:
    coordinator, repository, registry, _, _ = _admission_system(tmp_path / 'state.sqlite')
    router = RuntimeQueryRouter(registry, _admission_coordinator=coordinator)
    admission = _conversation_admission('frozen-command')
    route = router.route_conversation_query(admission=admission)
    envelope = RunEnvelopeV2.model_validate(route.execution_snapshot['envelope'])
    assert repository.load_execution_snapshot(envelope)['runtime_input'] == route.execution_snapshot['runtime_input']
    assert PlatformToolContextFactory(repository)(envelope).to_term_record(envelope)
    repeated = router.route_conversation_query(admission=admission)
    assert repeated.execution_snapshot == repository.load_execution_snapshot(envelope)


def test_frozen_route_still_rejects_changed_user_intent(tmp_path: Path) -> None:
    coordinator, repository, registry, _, _ = _admission_system(tmp_path / 'state.sqlite')
    router = RuntimeQueryRouter(registry, _admission_coordinator=coordinator)
    admission = _conversation_admission('frozen-command')
    first = router.route_conversation_query(admission=admission)
    changed = deepcopy(admission)
    object.__setattr__(changed, 'model', 'another-model')
    with pytest.raises(RuntimeAdmissionConflict):
        router.route_conversation_query(admission=changed)
    assert repository.get_execution_snapshot(admission.session_id, admission.runtime_command_id)['resolved_model'] == first.execution_snapshot['resolved_model']


def test_frozen_route_rejects_changed_credential_reference_without_reading_credentials(tmp_path: Path) -> None:
    coordinator, _, registry, _, _ = _admission_system(tmp_path / 'state.sqlite')
    router = RuntimeQueryRouter(registry, _admission_coordinator=coordinator)
    admission = _conversation_admission('credential-ref-command')
    from workbench.models.profiles import ProviderProfileRecord
    first_provider = ProviderProfileRecord.model_validate({**admission.provider.model_dump(), 'credential_mode': 'reference', 'secret_id': 'credential-reference-one'})
    object.__setattr__(admission, 'provider', first_provider)
    router.route_conversation_query(admission=admission)
    changed = deepcopy(admission)
    object.__setattr__(changed, 'provider', ProviderProfileRecord.model_validate({**changed.provider.model_dump(), 'secret_id': 'credential-reference-two'}))
    with pytest.raises(RuntimeAdmissionConflict):
        router.route_conversation_query(admission=changed)


@pytest.mark.parametrize('runtime_id', ['goose', 'dsh'])
def test_artifact_policy_requires_composed_pool_and_runtime_tools_capability(tmp_path: Path, runtime_id: str) -> None:
    from tests.fixtures.host_v2 import runtime_capabilities
    coordinator, _, registry, _, _ = _admission_system(tmp_path / 'state.sqlite')
    router = RuntimeQueryRouter(registry, _admission_coordinator=coordinator, _platform_tools_enabled=True)
    admission = _conversation_admission('artifact-command')
    registry.register(runtime_capabilities(runtime_id, build_id='native:test', query=True, model=True), status='ready')
    _, disabled, _ = router._conversation_query(admission=admission, runtime_id=runtime_id, build_id='native:test')
    assert not disabled.tool_manifest
    registry.register(runtime_capabilities(runtime_id, build_id='native:test', query=True, model=True, tools=True), status='ready')
    command, enabled, runtime_input = router._conversation_query(admission=admission, runtime_id=runtime_id, build_id='native:test')
    assert enabled.tool_manifest == ARTIFACT_TOOL_MANIFEST
    from workbench.runtime.conversation_execution import build_runtime_execution_snapshot
    snapshot = build_runtime_execution_snapshot(admission, command, enabled, runtime_input)
    assert snapshot['permission_policy'] == {'tool_policy': 'allow', 'filesystem_policy': 'deny'}
    assert snapshot['effect_scope']['write_effects'] is True
    assert snapshot['effect_scope']['allowed_tool_ids'] == ('artifact.publish', 'artifact.read')
    assert enabled.workspace_grant.writable_paths == ()
    uncomposed = RuntimeQueryRouter(registry, _admission_coordinator=coordinator)
    assert not uncomposed._conversation_query(admission=admission, runtime_id=runtime_id, build_id='native:test')[1].tool_manifest


def test_application_injects_the_same_pool_into_both_native_clients_while_vault_locked(tmp_path: Path, monkeypatch) -> None:
    import workbench.main as main
    from types import SimpleNamespace
    from workbench.settings import WorkbenchSettings, RuntimeProcessConfig
    from workbench.runtime.engine_host.v2.platform_tool_pool import PlatformToolPool
    captured = {}
    clients = []
    def supervisor_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()
    def client_factory(*args, **kwargs):
        clients.append((args, kwargs))
        return SimpleNamespace()
    monkeypatch.setattr(main, 'SidecarSupervisor', supervisor_factory)
    monkeypatch.setattr(main, 'EngineHostV2Client', client_factory)
    monkeypatch.setattr(main, 'create_app', lambda settings: SimpleNamespace(state=SimpleNamespace()))
    runtimes = tuple(RuntimeProcessConfig(runtime_id=runtime_id, argv=('unused-test-host',)) for runtime_id in ('goose', 'dsh'))
    settings = WorkbenchSettings(runtime_dir=tmp_path, engine_host_v2_enabled=True, engine_host_v2_runtimes=runtimes)
    app = main.build_app(settings)
    assert isinstance(app.state.platform_tool_pool, PlatformToolPool)
    for runtime in runtimes:
        captured['client_factory'](runtime, 3, tmp_path / (runtime.runtime_id + '.lock'))
    assert len(clients) == 2
    assert all(item[1]['tool_executor'] is app.state.platform_tool_pool for item in clients)
    assert all(item[1]['provider_grant_transport'] for item in clients)
    # Composition does not demand credentials or spawn the sidecars.
    assert not settings.vault_path.exists()
