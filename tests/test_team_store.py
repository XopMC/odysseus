"""Durable team invariants, exercised against real SQLite and concurrent clients."""
import concurrent.futures
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from src.team_store import TeamStore, NotFound, Conflict, LeaseLost, BudgetError


class TeamStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "team.sqlite"
        self.now = 1000.0
        self.store = TeamStore(self.path, resource_capacities={"jetson": 1, "mac": 2},
                               clock=lambda: self.now)

    def task(self, **kwargs):
        return self.store.create_task("alice", "Implement and verify", **kwargs)

    def worker(self, task, **kwargs):
        return self.store.add_worker("alice", task["id"], "worker", **kwargs)

    def claim(self, task, **kwargs):
        return self.store.claim_worker("alice", task["id"], lease_seconds=10, **kwargs)

    def paid(self):
        task = self.task(budget_microusd=100)
        worker = self.worker(task)
        claim = self.claim(task)
        self.store.approve_endpoint("alice", task["id"], "paid", 100, 1_000_000, 1_000_000)
        return task, worker, claim

    def test_lazy_init_and_separate_restart(self):
        self.assertFalse(self.path.exists())
        task = self.task(metadata={"session_id": "session-1"})
        another = TeamStore(self.path)
        self.assertEqual(another.get_task("alice", task["id"])["metadata"]["session_id"], "session-1")
        self.assertEqual(another.schema_version(), 1)

    def test_explicit_unknown_result_blocks_new_identity_until_reconciled(self):
        for effectful in (False, True):
            with self.subTest(effectful=effectful):
                task = self.task()
                worker = self.worker(task)
                claim = self.claim(task)
                token = claim['lease_token']
                intent = self.store.record_tool_intent('alice', task['id'], worker['id'], token,
                    'remote_tool', {}, effectful=effectful, idempotency_key='first')
                self.store.record_tool_result('alice', task['id'], intent['id'], token,
                    {'exit_code': 1, 'outcome_unknown': True, 'retryable': False})
                reopened = TeamStore(self.path, clock=lambda: self.now)
                with self.assertRaises(NotFound):
                    reopened.get_tool_intent('bob', task['id'], intent['id'])
                self.assertEqual(reopened.get_tool_intent('alice', task['id'], intent['id'])['status'], 'unknown')
                with self.assertRaises(Conflict):
                    reopened.record_tool_intent('alice', task['id'], worker['id'], token,
                        'remote_tool', {}, effectful=effectful, idempotency_key='new-model-call-id')
                reopened.resolve_tool_intent('alice', task['id'], intent['id'], {'checked': True}, status='done')
                allowed = reopened.record_tool_intent('alice', task['id'], worker['id'], token,
                    'next_tool', {}, effectful=False, idempotency_key='after-review')
                self.assertTrue(allowed['created'])

    def test_owner_scope_and_metadata_merge(self):
        task = self.task(metadata={"keep": 1})
        worker = self.worker(task)
        self.store.update_task_metadata("alice", task["id"], {"phase": "coding"})
        self.assertEqual(self.store.get_task("alice", task["id"])["metadata"], {"keep": 1, "phase": "coding"})
        self.assertEqual(self.store.list_tasks("bob"), [])
        for operation in (
            lambda: self.store.get_task("bob", task["id"]),
            lambda: self.store.get_worker("bob", task["id"], worker["id"]),
            lambda: self.store.claim_worker("bob", task["id"]),
            lambda: self.store.events("bob", task["id"]),
            lambda: self.store.update_task_metadata("bob", task["id"], {}),
        ):
            with self.assertRaises(NotFound):
                operation()

    def test_concurrent_claims_obey_four_worker_cap(self):
        task = self.task(max_workers=4)
        for _ in range(12):
            self.worker(task)
        def attempt(_):
            return TeamStore(self.path, clock=lambda: self.now).claim_worker("alice", task["id"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(attempt, range(12)))
        claimed = [claim for claim in claims if claim]
        self.assertEqual(len(claimed), 4)
        self.assertEqual(len({c["id"] for c in claimed}), 4)

    def test_twenty_independent_backends_without_team_ceiling(self):
        task = self.task()
        for index in range(20):
            group = f'backend-{index}'
            self.store.configure_resource_group(group, 1)
            self.worker(task, resource_group=group)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            claims = list(pool.map(lambda _: TeamStore(self.path).claim_worker('alice', task['id']), range(20)))
        self.assertEqual(len([c for c in claims if c]), 20)
        self.assertEqual(len({c['id'] for c in claims}), 20)
        self.worker(task, resource_group='backend-0')
        self.assertIsNone(self.claim(task))

    def test_global_resource_capacity_across_tasks_and_owners(self):
        first = self.task()
        second = self.store.create_task("bob", "Other task")
        self.worker(first, resource_group="jetson")
        self.store.add_worker("bob", second["id"], "worker", resource_group="jetson")
        self.assertIsNotNone(self.claim(first))
        self.assertIsNone(self.store.claim_worker("bob", second["id"]))

    def test_unknown_read_holds_backend_until_reconciled_but_not_other_backend(self):
        first, second, independent = self.task(), self.task(), self.task()
        worker = self.worker(first, resource_group='jetson')
        self.worker(second, resource_group='jetson')
        self.worker(independent, resource_group='mac')
        claim = self.claim(first)
        intent = self.store.record_tool_intent('alice', first['id'], worker['id'], claim['lease_token'],
            'remote_read', {}, effectful=False)
        self.store.record_tool_result('alice', first['id'], intent['id'], claim['lease_token'],
            {'exit_code': 1, 'outcome_unknown': True})
        self.store.stop_worker('alice', first['id'], worker['id'])
        self.assertIsNone(self.claim(second))
        self.assertIsNotNone(self.claim(independent))
        self.store.resolve_tool_intent('alice', first['id'], intent['id'], {'observed': 'finished'})
        self.assertIsNotNone(self.claim(second))

    def test_multiple_resource_groups_lock_project_across_backends(self):
        self.store.configure_resource_group("project:/work/plain", 1)
        one, two = self.task(), self.task()
        profile = {"resource_groups": ["project:/work/plain"]}
        self.worker(one, resource_group="jetson", profile=profile)
        self.worker(two, resource_group="mac", profile=profile)
        self.assertIsNotNone(self.claim(one))
        self.assertIsNone(self.claim(two))

    def test_unknown_profile_group_fails_closed_and_reassignment_is_atomic(self):
        task = self.task()
        with self.assertRaises(Conflict):
            self.worker(task, profile={"resource_groups": ["unconfigured"]})
        self.assertEqual(self.store.list_workers("alice", task["id"]), [])
        worker = self.worker(task, resource_group="mac")
        with self.assertRaises(Conflict):
            self.store.update_worker("alice", task["id"], worker["id"], profile={"resource_groups": ["unconfigured"]})
        self.assertEqual(self.store.get_worker("alice", task["id"], worker["id"])["profile"], {})

    def test_expired_unknown_effect_blocks_other_task_before_recover_runs(self):
        one, two = self.task(), self.task()
        first = self.worker(one, resource_group="jetson")
        self.worker(two, resource_group="jetson")
        claim = self.claim(one)
        self.store.record_tool_intent("alice", one["id"], first["id"], claim["lease_token"],
                                     "edit_file", {}, effectful=True)
        self.now += 11
        self.assertIsNone(self.claim(two))

    def test_dependency_requires_acceptance_and_cycles_rejected(self):
        task = self.task()
        one = self.worker(task)
        two = self.worker(task, depends_on=[one["id"]])
        with self.assertRaises(Conflict):
            self.store.set_dependencies("alice", task["id"], one["id"], [two["id"]])
        claim = self.claim(task)
        self.store.finish_worker("alice", task["id"], one["id"], claim["lease_token"], {"answer": "candidate"})
        self.assertIsNone(self.claim(task))
        self.store.accept_worker("alice", task["id"], one["id"])
        self.assertEqual(self.claim(task)["id"], two["id"])

    def test_restart_recovery_and_stale_lease_fencing(self):
        task = self.task()
        worker = self.worker(task)
        first = self.claim(task)
        self.store.save_checkpoint("alice", task["id"], worker["id"], first["lease_token"], {"step": 2})
        self.now += 11
        restarted = TeamStore(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.recover("alice", task["id"])["requeued"], 1)
        second = restarted.claim_worker("alice", task["id"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(restarted.load_checkpoint("alice", task["id"], worker["id"])["payload"], {"step": 2})
        with self.assertRaises(LeaseLost):
            restarted.finish_worker("alice", task["id"], worker["id"], first["lease_token"], {})

    def test_lease_renewal_is_fenced_and_extends(self):
        task = self.task()
        worker = self.worker(task)
        claim = self.claim(task)
        self.now += 9
        renewed = self.store.renew_lease("alice", task["id"], worker["id"], claim["lease_token"], lease_seconds=20)
        self.assertEqual(renewed["lease_expires"], 1029)
        with self.assertRaises(LeaseLost):
            self.store.renew_lease("alice", task["id"], worker["id"], "wrong")

    def test_capacity_reconfiguration_never_evicts_running_work(self):
        task = self.task()
        self.worker(task, resource_group="mac")
        self.worker(task, resource_group="mac")
        self.claim(task); self.claim(task)
        with self.assertRaises(Conflict):
            self.store.configure_resource_group("mac", 1)

    def test_unknown_effect_blocks_recovery_and_keeps_resource(self):
        task = self.task()
        worker = self.worker(task, resource_group="jetson")
        claim = self.claim(task)
        intent = self.store.record_tool_intent("alice", task["id"], worker["id"], claim["lease_token"],
                                              "write_file", {"path": "file"}, effectful=True,
                                              idempotency_key="write-1")
        self.now += 11
        self.assertEqual(self.store.recover("alice", task["id"])["blocked"], 1)
        self.assertEqual(self.store.get_worker("alice", task["id"], worker["id"])["status"], "blocked")
        other = self.task()
        self.worker(other, resource_group="jetson")
        self.assertIsNone(self.claim(other))
        with self.assertRaises(LeaseLost):
            self.store.record_tool_result("alice", task["id"], intent["id"], claim["lease_token"], {"ok": True})
        self.store.resolve_tool_intent("alice", task["id"], intent["id"], {"verified": "not run"}, status="not_run")
        self.assertIsNotNone(self.claim(other))

    def test_completed_tool_result_survives_without_becoming_unknown(self):
        task = self.task()
        worker = self.worker(task)
        claim = self.claim(task)
        intent = self.store.record_tool_intent("alice", task["id"], worker["id"], claim["lease_token"],
                                              "write_file", {}, effectful=True, idempotency_key="write-1")
        self.store.record_tool_result("alice", task["id"], intent["id"], claim["lease_token"], {"ok": True})
        self.now += 11
        self.assertEqual(self.store.recover("alice", task["id"])["requeued"], 1)
        self.assertEqual(self.store.list_tool_intents("alice", task["id"])[0]["status"], "done")

    def test_intent_idempotency_across_attempts_never_authorizes_replay(self):
        task = self.task()
        worker = self.worker(task)
        first = self.claim(task)
        intent = self.store.record_tool_intent("alice", task["id"], worker["id"], first["lease_token"],
                                              "write_file", {"path": "a", "text": "b"},
                                              effectful=True, idempotency_key="step-1")
        self.assertTrue(intent["created"])
        self.store.record_tool_result("alice", task["id"], intent["id"], first["lease_token"], {"ok": True})
        self.now += 11
        second = self.claim(task)
        replay = self.store.record_tool_intent("alice", task["id"], worker["id"], second["lease_token"],
                                              "write_file", {"text": "b", "path": "a"},
                                              effectful=True, idempotency_key="step-1")
        self.assertFalse(replay["created"])
        self.assertEqual(replay["status"], "done")

    def test_live_task_reads_do_not_expose_lease_fencing_token(self):
        task = self.task()
        worker = self.worker(task)
        claim = self.claim(task)
        self.assertTrue(claim["lease_token"])
        self.assertNotIn("lease_token", self.store.get_worker("alice", task["id"], worker["id"]))
        self.assertNotIn("lease_token", self.store.list_attempts("alice", task["id"], worker["id"])[0])

    def test_stale_checkpoint_and_artifact_cannot_overwrite_new_attempt(self):
        task = self.task()
        worker = self.worker(task)
        first = self.claim(task)
        self.now += 11
        second = self.claim(task)
        with self.assertRaises(LeaseLost):
            self.store.save_checkpoint("alice", task["id"], worker["id"], first["lease_token"], {"bad": True})
        with self.assertRaises(LeaseLost):
            self.store.add_artifact("alice", task["id"], "stale", {}, worker_id=worker["id"], lease_token=first["lease_token"])
        self.store.save_checkpoint("alice", task["id"], worker["id"], second["lease_token"], {"good": True})

    def test_events_monotonic_bounded_resume(self):
        task = self.task()
        for n in range(8):
            self.store.add_event("alice", task["id"], "progress", {"n": n})
        first = self.store.events("alice", task["id"], limit=3)
        rest = self.store.events("alice", task["id"], after_seq=first[-1]["seq"])
        self.assertEqual([e["seq"] for e in first + rest], list(range(1, 10)))

    def test_pause_cancel_and_idle_only_reassignment(self):
        task = self.task()
        worker = self.worker(task)
        self.store.update_worker("alice", task["id"], worker["id"], profile={"model": "new"})
        self.store.set_task_status("alice", task["id"], "paused")
        self.assertIsNone(self.claim(task))
        self.store.set_task_status("alice", task["id"], "running")
        claim = self.claim(task)
        with self.assertRaises(Conflict):
            self.store.update_worker("alice", task["id"], worker["id"], name="renamed")
        self.store.set_task_status("alice", task["id"], "cancelled")
        with self.assertRaises(LeaseLost):
            self.store.finish_worker("alice", task["id"], worker["id"], claim["lease_token"], {})

    def test_structured_credentials_rejected_and_profiles_artifacts_owner_scoped(self):
        task = self.task()
        with self.assertRaises(ValueError):
            self.store.update_task_metadata("alice", task["id"], {"nested": {"api_key": "secret"}})
        self.store.save_profile("alice", "coder", {"endpoint_id": "opaque", "model": "local"})
        self.assertEqual(self.store.list_profiles("bob"), [])
        artifact = self.store.add_artifact("alice", task["id"], "report", {"path": "report.md"})
        self.assertEqual(self.store.list_artifacts("alice", task["id"])[0]["id"], artifact["id"])
        with self.assertRaises(NotFound):
            self.store.list_artifacts("bob", task["id"])

    def test_budget_requires_rates_and_approval(self):
        task = self.task(budget_microusd=100)
        worker = self.worker(task)
        claim = self.claim(task)
        with self.assertRaises(BudgetError):
            self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 10, 20)

    def test_concurrent_reservations_cannot_overspend(self):
        task, worker, claim = self.paid()
        def reserve(_):
            try:
                return self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 10, 30)
            except BudgetError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            reservations = [r for r in pool.map(reserve, range(10)) if r]
        self.assertEqual(len(reservations), 2)
        self.assertEqual(self.store.get_task("alice", task["id"])["reserved_microusd"], 80)

    def test_revoke_blocks_reserve_and_sending_unsent_reservation(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        self.store.revoke_endpoint("alice", task["id"], "paid")
        with self.assertRaises(BudgetError):
            self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        with self.assertRaises(BudgetError):
            self.store.mark_sent("alice", task["id"], reservation["id"], claim["lease_token"])
        self.store.release("alice", task["id"], reservation["id"])
        self.assertEqual(self.store.get_task("alice", task["id"])["reserved_microusd"], 0)

    def test_sent_cannot_release_and_unknown_settlement_charges_ceiling_once(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        self.store.mark_sent("alice", task["id"], reservation["id"], claim["lease_token"])
        with self.assertRaises(Conflict):
            self.store.release("alice", task["id"], reservation["id"])
        self.store.settle_unknown("alice", task["id"], reservation["id"])
        self.store.settle_unknown("alice", task["id"], reservation["id"])
        saved = self.store.get_task("alice", task["id"])
        self.assertEqual((saved["spent_microusd"], saved["reserved_microusd"]), (25, 0))

    def test_recovery_returns_unsent_money_but_preserves_sent_uncertainty(self):
        task, worker, claim = self.paid()
        unsent = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        sent = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        self.store.mark_sent("alice", task["id"], sent["id"], claim["lease_token"])
        self.now += 11
        restarted = TeamStore(self.path, clock=lambda: self.now)
        restarted.recover("alice", task["id"])
        states = {r["id"]: r["status"] for r in restarted.list_reservations("alice", task["id"])}
        self.assertEqual(states, {unsent["id"]: "released", sent["id"]: "sent"})
        self.assertEqual(restarted.get_task("alice", task["id"])["reserved_microusd"], 25)
        restarted.settle_unknown("alice", task["id"], sent["id"])
        self.assertEqual(restarted.get_task("alice", task["id"])["spent_microusd"], 25)

    def test_budget_ceiling_cannot_be_lowered_under_committed_money(self):
        task, worker, claim = self.paid()
        self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        with self.assertRaises(BudgetError):
            self.store.set_task_budget("alice", task["id"], 24)
        with self.assertRaises(BudgetError):
            self.store.approve_endpoint("alice", task["id"], "paid", 24, 1, 1)

    def test_concurrent_settlement_is_idempotent(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        self.store.mark_sent("alice", task["id"], reservation["id"], claim["lease_token"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.store.settle("alice", task["id"], reservation["id"], 5, 10), range(8)))
        self.assertEqual(self.store.get_task("alice", task["id"])["spent_microusd"], 15)

    def test_settlement_refunds_unused_reservation_and_rejects_overrun(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        self.store.mark_sent("alice", task["id"], reservation["id"], claim["lease_token"])
        with self.assertRaises(BudgetError):
            self.store.settle("alice", task["id"], reservation["id"], 5, 100)
        self.store.settle("alice", task["id"], reservation["id"], 5, 10)
        self.store.settle("alice", task["id"], reservation["id"], 5, 10)
        self.assertEqual(self.store.get_task("alice", task["id"])["spent_microusd"], 15)
        with self.assertRaises(Conflict):
            self.store.settle("alice", task["id"], reservation["id"], 5, 11)

    def test_shared_task_budget_limits_multiple_endpoints(self):
        task, worker, claim = self.paid()
        self.store.approve_endpoint("alice", task["id"], "other", 100, 1_000_000, 1_000_000)
        self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 10, 60)
        with self.assertRaises(BudgetError):
            self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "other", 10, 30)

    def test_cost_rounding_uses_integer_microdollars(self):
        task = self.task(budget_microusd=2)
        worker, claim = self.worker(task), self.claim(task)
        self.store.approve_endpoint("alice", task["id"], "small", 2, 1, 1)
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "small", 1, 1)
        self.assertEqual(reservation["reserved_microusd"], 1)

    def test_wrong_owner_cannot_mutate_cost_or_read_checkpoint(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        for operation in (
            lambda: self.store.release("bob", task["id"], reservation["id"]),
            lambda: self.store.settle_unknown("bob", task["id"], reservation["id"]),
            lambda: self.store.load_checkpoint("bob", task["id"], worker["id"]),
            lambda: self.store.revoke_endpoint("bob", task["id"], "paid"),
        ):
            with self.assertRaises(NotFound):
                operation()

    def test_concurrent_first_use_initializes_additively(self):
        def create(index):
            return TeamStore(self.path).create_task("alice", f"task-{index}")["id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(create, range(8)))
        self.assertEqual(len(set(ids)), 8)

    def test_schema_version_guard_preserves_existing_database(self):
        import sqlite3
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('keep')")
            connection.execute("PRAGMA user_version=99")
        with self.assertRaises(Conflict):
            self.task()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_new_schema_is_additive_and_does_not_touch_other_tables(self):
        import sqlite3
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('keep')")
        self.task()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")

    def test_coordinator_concurrent_claim_restart_and_stale_fencing(self):
        task = self.task()
        def claim(_):
            return TeamStore(self.path, clock=lambda: self.now).claim_coordinator("alice", task["id"], lease_seconds=10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = [c for c in pool.map(claim, range(8)) if c]
        self.assertEqual(len(claims), 1)
        token = claims[0]["lease_token"]
        self.store.update_task_metadata("alice", task["id"], {"phase": "working"}, coordinator_token=token)
        self.now += 11
        replacement = claim(0)
        self.assertNotEqual(replacement["lease_token"], token)
        for operation in (
            lambda: self.store.update_task_metadata("alice", task["id"], {}, coordinator_token=token),
            lambda: self.store.add_worker("alice", task["id"], "stale", coordinator_token=token),
            lambda: self.store.add_event("alice", task["id"], "stale", {}, coordinator_token=token),
            lambda: self.store.release_coordinator("alice", task["id"], token),
            lambda: self.store.renew_coordinator("alice", task["id"], token),
        ):
            with self.assertRaises(LeaseLost):
                operation()
        self.assertEqual(self.store.list_workers("alice", task["id"]), [])
        with self.assertRaises(NotFound):
            self.store.renew_coordinator("bob", task["id"], replacement["lease_token"])
        self.store.renew_coordinator("alice", task["id"], replacement["lease_token"], lease_seconds=30)
        self.store.release_coordinator("alice", task["id"], replacement["lease_token"])
        self.assertIsNotNone(claim(0))

    def test_acceptance_does_not_finish_team_before_coordinator_final_checks(self):
        task = self.task()
        worker, claim = self.worker(task), self.claim(task)
        self.store.finish_worker("alice", task["id"], worker["id"], claim["lease_token"], {"ok": True})
        coordinator = self.store.claim_coordinator("alice", task["id"])
        token = coordinator["lease_token"]
        self.store.accept_worker("alice", task["id"], worker["id"], coordinator_token=token)
        self.assertEqual(self.store.get_task("alice", task["id"])["status"], "running")
        artifact = self.store.add_artifact("alice", task["id"], "reviewed", {}, worker_id=worker["id"], coordinator_token=token)
        self.assertEqual(artifact["worker_id"], worker["id"])
        self.store.set_task_status("alice", task["id"], "done", coordinator_token=token)

    def test_stop_worker_fences_running_result_and_does_not_replay_unknown_effect(self):
        task = self.task()
        worker, claim = self.worker(task), self.claim(task)
        self.store.stop_worker("alice", task["id"], worker["id"], status="paused")
        with self.assertRaises(LeaseLost):
            self.store.finish_worker("alice", task["id"], worker["id"], claim["lease_token"], {})
        self.assertIsNone(self.claim(task))
        self.store.update_worker("alice", task["id"], worker["id"], status="pending")
        claim = self.claim(task)
        intent = self.store.record_tool_intent("alice", task["id"], worker["id"], claim["lease_token"], "write", {}, effectful=True)
        stopped = self.store.stop_worker("alice", task["id"], worker["id"], status="cancelled")
        self.assertEqual(stopped["status"], "blocked")
        with self.assertRaises(Conflict):
            self.store.update_worker("alice", task["id"], worker["id"], status="pending")
        self.store.resolve_tool_intent("alice", task["id"], intent['id'], {"verified": "not run"}, status="not_run")
        self.assertEqual(self.store.get_worker("alice", task["id"], worker["id"])["status"], "cancelled")
        self.assertIsNone(self.claim(task))

    def test_pause_invalidates_coordinator_before_late_mutation(self):
        task = self.task()
        token = self.store.claim_coordinator("alice", task["id"])["lease_token"]
        self.store.set_task_status("alice", task["id"], "paused")
        with self.assertRaises(LeaseLost):
            self.store.add_worker("alice", task["id"], "late planner", coordinator_token=token)
        self.assertIsNone(self.store.claim_coordinator("alice", task["id"]))

    def test_budget_status_is_owner_scoped_consistent_snapshot(self):
        task, worker, claim = self.paid()
        reservation = self.store.reserve("alice", task["id"], worker["id"], claim["lease_token"], "paid", 5, 20)
        summary = self.store.budget_status("alice", task["id"])
        self.assertEqual(summary["budget_microusd"], 100)
        self.assertEqual(summary["reserved_microusd"], 25)
        self.assertEqual(summary["remaining_microusd"], 75)
        self.assertEqual(summary["approvals"][0]["endpoint_id"], "paid")
        self.assertEqual(summary["reservations"][0]["id"], reservation["id"])
        with self.assertRaises(NotFound):
            self.store.budget_status("bob", task["id"])


if __name__ == "__main__":
    unittest.main()
