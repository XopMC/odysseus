import hashlib

import pytest

from src.engineering_checks import EngineeringChecks
from src.engineering_store import EngineeringStore
from src.team_store import Conflict, NotFound, TeamStore


A = hashlib.sha256(b'original tree').hexdigest()
B = hashlib.sha256(b'changed tree').hexdigest()


@pytest.fixture
def ctx(tmp_path):
    team = TeamStore(tmp_path / 'teams.db')
    projects = EngineeringStore(team)
    project = projects.create_project('owner', name='Project', root='/tmp/project', host_id='host')
    projects.set_policy('owner', project['id'], 1, 'trusted_host', confirmation=True)
    authenticated = {}
    def verify(expected, evidence):
        return authenticated.get((expected['owner'], expected['id'])) == evidence
    checks = EngineeringChecks(team, verify_evidence=verify)
    profile = checks.approve_profile('owner', project['id'], name='Tests', command='python -m pytest', confirmation=True)
    checks.set_requirement('owner', project['id'], title='Tests pass', profile_ids=[profile['id']])
    return team, projects, project['id'], checks, profile, authenticated


def finish(ctx, *, kind='check', code=0, after=A, terminal_status=None,
           toolchain='python 3.12 / pytest 8'):
    _, _, project, checks, profile, authenticated = ctx
    run = checks.start_run('owner', project, profile['id'], A, kind=kind)
    evidence = {'run_id': run['id'], 'host_id': 'host', 'command_hash': profile['command_hash'],
                'workspace_hash': A, 'workspace_hash_after': after, 'exit_code': code,
                'runner_job_id': 'job-' + run['id'], 'toolchain': toolchain, 'protocol': 1}
    if terminal_status is not None:
        evidence['terminal_status'] = terminal_status
    authenticated[('owner', run['id'])] = evidence
    return checks.finish_run('owner', project, run['id'], evidence), evidence


def test_authenticated_cancelled_zero_exit_is_not_success(ctx):
    _, _, project, checks, _, _ = ctx
    result, evidence = finish(ctx, code=0, terminal_status='cancelled')
    assert result['status'] == 'cancelled'
    assert not checks.readiness('owner', project, A)['ready']
    assert checks.finish_run('owner', project, result['id'], evidence)['status'] == 'cancelled'
    events = checks.projects.events('owner', project)
    assert len([event for event in events if event['type'] == 'check_finished']) == 1


def test_requirement_pages_keep_all_criteria_and_reject_foreign_cursor(ctx):
    _, projects, project, checks, profile, _ = ctx
    for index in range(104):
        checks.set_requirement('owner', project, title='Criterion ' + str(index), profile_ids=[profile['id']])
    seen, cursor = [], ''
    while True:
        page = checks.list_requirements('owner', project, after_id=cursor, limit=50)
        seen.extend(row['id'] for row in page['requirements'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 105
    other = projects.create_project('owner', name='Other', root='/work/other', host_id='host')
    with pytest.raises(NotFound):
        checks.list_requirements('owner', other['id'], after_id=seen[0])
    with pytest.raises(NotFound):
        checks.list_requirements('attacker', project)


def test_readiness_snapshot_binds_criteria_profiles_and_checked_code(ctx):
    _, _, project, checks, profile, _ = ctx
    finish(ctx)
    initial = checks.readiness('owner', project, A)
    assert initial['ready']
    assert len(initial['snapshot_id']) == 64
    assert checks.readiness('owner', project, A)['snapshot_id'] == initial['snapshot_id']
    criterion = checks.list_requirements('owner', project)['requirements'][0]
    checks.set_requirement('owner', project, title='Reviewed wording', profile_ids=criterion['profile_ids'],
                           requirement_id=criterion['id'], expected_revision=criterion['revision'])
    updated = checks.readiness('owner', project, A)
    assert updated['snapshot_id'] != initial['snapshot_id']
    assert updated['requirements'][0]['revision'] == 2
    # The same approved check can still prove a reworded criterion, but the old
    # readiness snapshot no longer identifies the current criteria set.
    assert updated['ready']
    assert checks.readiness('owner', project, B)['ready'] is False
    checks.approve_profile('owner', project, name='New tests', command='true', confirmation=True,
                           profile_id=profile['id'], expected_revision=profile['revision'])
    changed = checks.readiness('owner', project, A)
    assert changed['ready'] is False
    assert changed['snapshot_id'] != updated['snapshot_id']
    assert changed['requirements'][0]['checks'][0]['profile_revision'] == 2


@pytest.mark.parametrize('before_code,after_code,classification', [
    (0, 0, 'remained_passing'), (0, 7, 'became_failing'),
    (7, 0, 'became_passing'), (7, 8, 'failure_persists')])
def test_baseline_comparison_is_command_outcome_not_claim_of_identical_failures(ctx, before_code, after_code, classification):
    _, _, project, checks, _, _ = ctx
    before, _ = finish(ctx, kind='baseline', code=before_code)
    after, _ = finish(ctx, code=after_code)
    result = checks.compare_baseline('owner', project, before['id'], after['id'])
    assert result['classification'] == classification
    assert result['individual_failures_compared'] is False
    assert result['scope'] == 'command_outcome_only'
    assert result['before']['exit_code'] == before_code
    assert result['after']['exit_code'] == after_code
    with pytest.raises(NotFound):
        checks.compare_baseline('attacker', project, before['id'], after['id'])


def test_baseline_comparison_rejects_changed_profile_and_incomplete_evidence(ctx):
    _, _, project, checks, profile, _ = ctx
    before, _ = finish(ctx, kind='baseline')
    running = checks.start_run('owner', project, profile['id'], A)
    result = checks.compare_baseline('owner', project, before['id'], running['id'])
    assert result['classification'] == 'not_comparable'
    checks.approve_profile('owner', project, name='Weakened check', command='true', confirmation=True,
                           profile_id=profile['id'], expected_revision=1)
    # Helper uses the stored current profile for dispatch, not its cached fixture.
    run = checks.start_run('owner', project, profile['id'], A)
    result = checks.compare_baseline('owner', project, before['id'], run['id'])
    assert 'profile_revision_changed' in result['reasons']
    assert 'command_hash_changed' in result['reasons']
    with pytest.raises(ValueError):
        checks.compare_baseline('owner', project, run['id'], before['id'])


def test_baseline_rejects_reported_environment_change_and_reverse_chronology(ctx):
    team, _, project, checks, _, _ = ctx
    before, _ = finish(ctx, kind='baseline')
    after, _ = finish(ctx, toolchain='python 3.13 / pytest 8')
    comparison = checks.compare_baseline('owner', project, before['id'], after['id'])
    assert comparison['classification'] == 'not_comparable'
    assert comparison['reasons'] == ['reported_environment_changed']
    assert comparison['before']['reported_toolchain'] == 'python 3.12 / pytest 8'
    assert comparison['after']['reported_toolchain'] == 'python 3.13 / pytest 8'
    with team._tx() as db:
        db.execute('UPDATE engineering_check_runs SET started_at=? WHERE id=?',
                   (after['started_at'] + 1, before['id']))
    with pytest.raises(Conflict, match='precede'):
        checks.compare_baseline('owner', project, before['id'], after['id'])


def test_check_history_keyset_pages_have_no_total_cap(ctx):
    _, projects, project, checks, profile, _ = ctx
    expected = {checks.start_run('owner', project, profile['id'], A)['id'] for _ in range(105)}
    records, cursor = [], ''
    while True:
        page = checks.list_runs('owner', project, limit=50, after_id=cursor)
        records.extend(row['id'] for row in page)
        if len(page) < 50:
            break
        cursor = page[-1]['id']
    assert len(records) == len(set(records)) == 105
    assert set(records) == expected
    other = projects.create_project('owner', name='Other', root='/work/other', host_id='host')
    with pytest.raises(NotFound):
        checks.list_runs('owner', other['id'], after_id=records[0])


def test_verified_success_persists_and_changed_code_is_stale(ctx):
    team, _, project, checks, _, _ = ctx
    with pytest.raises(Conflict):
        checks.assert_complete('owner', project, A)
    run, _ = finish(ctx)
    assert run['status'] == 'passed'
    assert checks.assert_complete('owner', project, A)['ready']
    reopened = EngineeringChecks(TeamStore(team.path))
    assert reopened.assert_complete('owner', project, A)['ready']
    with pytest.raises(Conflict):
        reopened.assert_complete('owner', project, B)
    assert reopened.list_runs('owner', project)[0]['workspace_hash'] == A


def test_baseline_failure_and_inflight_retry_cannot_complete(ctx):
    _, _, project, checks, profile, _ = ctx
    baseline, _ = finish(ctx, kind='baseline')
    assert baseline['status'] == 'passed'
    assert not checks.readiness('owner', project, A)['ready']
    finish(ctx)
    assert checks.readiness('owner', project, A)['ready']
    checks.start_run('owner', project, profile['id'], A)
    assert not checks.readiness('owner', project, A)['ready']
    failed, _ = finish(ctx, code=1)
    assert failed['status'] == 'failed'
    assert not checks.readiness('owner', project, A)['ready']


def test_cannot_forge_or_reassign_evidence(ctx):
    team, _, project, checks, profile, authenticated = ctx
    run, evidence = finish(ctx)
    assert checks.finish_run('owner', project, run['id'], evidence) == run
    other = checks.start_run('owner', project, profile['id'], A)
    with pytest.raises(Conflict):
        checks.finish_run('owner', project, other['id'], evidence)
    forged = dict(evidence, run_id=other['id'])
    with pytest.raises(PermissionError):
        checks.finish_run('owner', project, other['id'], forged)
    with pytest.raises(PermissionError):
        EngineeringChecks(team).finish_run('owner', project, other['id'], forged)
    with pytest.raises(ValueError):
        checks.finish_run('owner', project, other['id'], dict(forged, status='passed'))
    for field, value in [('host_id', 'other-host'), ('command_hash', B), ('workspace_hash', B)]:
        with pytest.raises(Conflict):
            checks.finish_run('owner', project, other['id'], dict(forged, **{field: value}))
    changed = dict(evidence, exit_code=1)
    authenticated[('owner', run['id'])] = changed
    with pytest.raises(Conflict):
        checks.finish_run('owner', project, run['id'], changed)


def test_policy_revocation_and_profile_revision_invalidate(ctx):
    _, projects, project, checks, profile, _ = ctx
    finish(ctx)
    with pytest.raises(PermissionError):
        checks.approve_profile('owner', project, name='Tests', command='true', confirmation=False)
    updated = checks.approve_profile('owner', project, name='New tests', command='python -m unittest',
                                    profile_id=profile['id'], expected_revision=1, confirmation=True)
    assert updated['revision'] == 2 and updated['command_hash'] != profile['command_hash']
    assert not checks.readiness('owner', project, A)['ready']
    with pytest.raises(Conflict):
        checks.approve_profile('owner', project, name='Old', command='true', profile_id=profile['id'],
                               expected_revision=1, confirmation=True)
    projects.set_policy('owner', project, 2, None, confirmation=True)
    with pytest.raises(PermissionError):
        checks.start_run('owner', project, profile['id'], A)


def test_workspace_changed_during_check_and_policy_changed_during_auth(ctx):
    _, projects, project, checks, _, authenticated = ctx
    stale, _ = finish(ctx, after=B)
    assert stale['status'] == 'stale'
    original_verify = checks.verify_evidence
    def revoke(expected, evidence):
        projects.set_policy('owner', project, 2, None, confirmation=True)
        return original_verify(expected, evidence)
    checks.verify_evidence = revoke
    stale, _ = finish(ctx)
    assert stale['status'] == 'stale'
    assert not checks.readiness('owner', project, A)['ready']


def test_owner_and_project_boundaries(ctx):
    _, projects, project, checks, profile, _ = ctx
    run, evidence = finish(ctx)
    other = projects.create_project('owner', name='Other', root='/tmp/other', host_id='host')
    projects.set_policy('owner', other['id'], 1, 'trusted_host', confirmation=True)
    for action in [lambda: checks.readiness('attacker', project, A),
                   lambda: checks.list_runs('attacker', project),
                   lambda: checks.finish_run('attacker', project, run['id'], evidence),
                   lambda: checks.start_run('owner', other['id'], profile['id'], A),
                   lambda: checks.set_requirement('owner', other['id'], title='Cross', profile_ids=[profile['id']])]:
        with pytest.raises(NotFound):
            action()
    assert not checks.readiness('owner', other['id'], A)['ready']


def test_requirements_all_mandatory_and_revision_cas(ctx):
    _, _, project, checks, _, _ = ctx
    finish(ctx)
    second = checks.approve_profile('owner', project, name='Lint', command='ruff check .', confirmation=True)
    req = checks.set_requirement('owner', project, title='Lint', profile_ids=[second['id']])
    assert not checks.readiness('owner', project, A)['ready']
    with pytest.raises(Conflict):
        checks.set_requirement('owner', project, title='Lint', profile_ids=[second['id']],
                               requirement_id=req['id'], expected_revision=0, mandatory=False)
    checks.set_requirement('owner', project, title='Optional lint', profile_ids=[second['id']],
                           requirement_id=req['id'], expected_revision=1, mandatory=False)
    assert checks.assert_complete('owner', project, A)['ready']


def test_schema_is_additive_and_inputs_bounded(ctx):
    team, _, project, checks, profile, _ = ctx
    legacy = team.create_task('owner', 'Legacy')
    finish(ctx)
    assert team.get_task('owner', legacy['id'])['title'] == 'Legacy'
    with pytest.raises(ValueError):
        checks.start_run('owner', project, profile['id'], 'model says unchanged')
    with pytest.raises(ValueError):
        checks.list_runs('owner', project, limit=10000)
    with pytest.raises(ValueError):
        checks.set_requirement('owner', project, title='No evidence', profile_ids=[])
