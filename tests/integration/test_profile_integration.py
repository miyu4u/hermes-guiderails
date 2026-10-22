"""Profile-state acceptance through the registered Hermes plugin boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from guiderails.plugin import register
from guiderails.tool_gate import ToolGate


def document(rule_id="test.initial", word="original", enabled=True):
    return {
        "schema_version": 1,
        "rules": [{
            "id": rule_id,
            "revision": 1,
            "enabled": enabled,
            "description": "Reject the specified literal.",
            "priority": 100,
            "scopes": ["tool.arguments", "artifact.text"],
            "matcher": {
                "type": "match", "value": word,
                "case_sensitive": True, "normalization": "nfc",
            },
            "edit_policy": "whole_result",
            "announce": {"instruction": "Use a specific alternative."},
            "tests": {"positive": [word], "negative": ["safe"]},
        }],
    }


class Context:
    def __init__(self, proposal=None):
        self.profile_name = "fixture"
        self.hooks, self.tools, self.commands = {}, {}, {}
        self.unload = []
        self.proposal = proposal or document("test.proposed", "forbidden")
        self.llm = SimpleNamespace(complete_structured=self.complete)

    def complete(self, **kwargs):
        return SimpleNamespace(parsed=self.proposal, text=json.dumps(self.proposal))

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_middleware(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, **kwargs):
        pass

    def on_unload(self, callback):
        self.unload.append(callback)


def local_context():
    return {"local": True, "base": None, "home": str(Path.home())}


def seed(home):
    patterns = home / "guiderails" / "patterns"
    patterns.mkdir(parents=True)
    (patterns / "initial.yaml").write_text(json.dumps(document()), encoding="utf-8")


def create_runtime(home, context=None, current_home=None):
    context = context or Context()
    runtime = register(context, home=home, current_home=current_home or (lambda: home),
                       file_context=local_context)
    return runtime, context


def model(context, action, **arguments):
    return json.loads(context.tools["guiderails_patterns"]["handler"](
        {"action": action, **arguments}
    ))


def command(context, arguments):
    return json.loads(asyncio.run(context.commands["guiderails"](arguments)))


def digest(context):
    return model(context, "status")["status"]["ruleset_digest"]


def inspect(context, text, *, request="request-1", session="session-1", call="call-1"):
    return context.hooks["pre_tool_call"](
        "write_file", {"path": str(context.target), "content": text},
        session_id=session, turn_id="turn-1", operation_id="operation-1",
        api_request_id=request, tool_call_id=call,
    )


def audit_events(home):
    directory = home / "guiderails" / "audit"
    paths = [directory / "events.jsonl", *sorted((directory / "archive").glob("*.jsonl"))]
    return [json.loads(line) for path in paths if path.exists()
            for line in path.read_text(encoding="utf-8").splitlines()]


class ProfileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "profile-a"
        seed(self.home)
        self.runtime, self.ctx = create_runtime(self.home)
        self.ctx.target = self.home / "output.txt"
        self.addCleanup(self.runtime.close)

    def propose(self):
        result = model(self.ctx, "propose", request="Reject forbidden.")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["approval_required"])
        return result["draft"]

    def test_rejection_budget_is_atomic_across_runtimes_and_survives_reload(self):
        root = self.home / "guiderails"
        (root / "config.yaml").write_text("schema_version: 1\nmax_rejections: 1\n", encoding="utf-8")
        first, second = create_runtime(self.home), create_runtime(self.home)
        for runtime, _ in (first, second):
            self.addCleanup(runtime.close)
            _ = runtime.manager

        def invoke(target, call, *, turn="turn-1", session="shared", value="original", name="example"):
            return target.hooks["pre_tool_call"](name, {"value": value}, session_id=session, turn_id=turn,
                                                 api_request_id=f"{turn}:api:1", tool_call_id=call)

        start = threading.Barrier(4)

        def concurrent(item):
            ctx, call = item
            start.wait(5)
            return call, invoke(ctx, call)

        with ThreadPoolExecutor(max_workers=4) as executor:
            calls = [(first[1], "same"), (second[1], "same"), (first[1], "distinct"), (second[1], "third")]
            outcomes = list(executor.map(concurrent, calls))
        duplicate = [decision for call, decision in outcomes if call == "same"]
        self.assertEqual(duplicate[0], duplicate[1])
        decisions = dict(outcomes)
        self.assertEqual(sum(decision is not None for decision in decisions.values()), 1)
        events = audit_events(self.home)
        self.assertCountEqual([event["decision"] for event in events], ["blocked", "warned_allowed", "warned_allowed"])

        original = document()["rules"][0]
        revised = dict(original, revision=2, keep=False, announce={"instruction": "Replacement guidance."})
        another = document("test.other", "new marker")["rules"][0]
        path = root / "patterns" / "initial.yaml"
        path.write_text(json.dumps({"schema_version": 1, "rules": [revised, another]}), encoding="utf-8")
        self.assertTrue(command(first[1], "reload")["ok"])
        self.assertIsNone(invoke(first[1], "new-turn", turn="turn-2", name="other_tool"))
        self.assertIsNotNone(invoke(first[1], "other-rule", turn="turn-2", value="new marker"))
        self.assertIsNotNone(invoke(first[1], "new-session", session="separate"))
        for index in range(2):
            self.assertIsNotNone(invoke(first[1], f"anonymous-{index}", session=""))
        malformed = first[1].hooks["pre_tool_call"](
            "write_file", {"path": False, "content": "original"}, session_id="shared", tool_call_id="invalid")
        self.assertIsNotNone(malformed)
        self.assertNotIn("guiderails.pattern_detected", malformed["message"])

    def test_audit_failure_preserves_the_persisted_pattern_decision(self):
        (self.home / "guiderails" / "config.yaml").write_text(
            "schema_version: 1\nmax_rejections: 1\n", encoding="utf-8")
        audit = self.runtime.manager.audit
        audit.path.write_text("broken audit\n", encoding="utf-8")
        decision = inspect(self.ctx, "original")
        self.assertIn("guiderails.pattern_detected", decision["message"])
        self.assertEqual(self.runtime.manager.store.stats()["blocked_attempt_count"], 1)
        self.assertIsNone(inspect(self.ctx, "original", call="next"))
        audit.path.unlink()
        self.assertIsNone(inspect(self.ctx, "original", call="last"))

    def test_reload_keeps_one_snapshot_for_each_hook_call(self):
        for shape in ("single", "multiple", "group"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                seed(home)
                runtime, ctx = create_runtime(home)
                self.addCleanup(runtime.close)
                engine = runtime.manager.engine
                engine.limits = replace(engine.limits, max_inspection_ms=10000)
                before = digest(ctx)
                if shape == "single":
                    args = {"value": "original new marker"}
                    expected_a, expected_b = "/value", "/value"
                elif shape == "multiple":
                    args = {"first": "safe", "second": "original", "third": "new marker"}
                    expected_a, expected_b = "/second", "/third"
                else:
                    args = {"calls": [
                        {"name": "example", "arguments": {"value": "safe"}},
                        {"name": "example", "arguments": {"value": "original"}},
                        {"name": "example", "arguments": {"value": "new marker"}},
                    ]}
                    expected_a = "/calls/1/arguments/value"
                    expected_b = "/calls/2/arguments/value"
                name = "tool_call" if shape == "group" else "example"
                entered, resume = threading.Event(), threading.Event()
                original_inspect = engine.inspect
                gate_inspect = ToolGate.inspect
                captured = []

                def pause_after_first(*values, **kwargs):
                    result = original_inspect(*values, **kwargs)
                    if not entered.is_set():
                        entered.set()
                        if not resume.wait(5):
                            raise RuntimeError("reload did not complete")
                    return result

                def capture(gate, *values, **kwargs):
                    result = gate_inspect(gate, *values, **kwargs)
                    captured.append(result[0])
                    return result

                def invoke(call):
                    return ctx.hooks["pre_tool_call"](
                        name, args, session_id="session-1", turn_id="turn-1",
                        operation_id="operation-1", api_request_id=call, tool_call_id=call)

                with patch.object(engine, "inspect", side_effect=pause_after_first), \
                     patch.object(ToolGate, "inspect", new=capture), \
                     ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(invoke, "before")
                    try:
                        self.assertTrue(entered.wait(5))
                        path = home / "guiderails" / "patterns" / "initial.yaml"
                        path.write_text(json.dumps(document("test.next", "new marker")), encoding="utf-8")
                        self.assertTrue(command(ctx, "reload")["ok"])
                        after = digest(ctx)
                        self.assertNotEqual(before, after)
                    finally:
                        resume.set()
                    decision_a = future.result(timeout=5)
                    decision_b = invoke("after")

                self.assertEqual(decision_a["message"].splitlines()[0], "guiderails.pattern_detected")
                self.assertEqual(decision_b["message"].splitlines()[0], "guiderails.pattern_detected")
                self.assertEqual([(hit.rule_id, hit.field_path) for hit in captured[0].hits],
                                 [("test.initial", expected_a)])
                self.assertEqual([(hit.rule_id, hit.field_path) for hit in captured[1].hits],
                                 [("test.next", expected_b)])
                self.assertEqual([result.ruleset_digest for result in captured], [before, after])
                for events in (audit_events(home), model(ctx, "report")["events"]):
                    detections = [(event["unit_id"], event["rule_ids"], event["ruleset_digest"])
                                  for event in events if event["event_type"] == "detected"]
                    self.assertCountEqual(detections, [("before", ["test.initial"], before),
                                                       ("after", ["test.next"], after)])

    def test_approval_commits_with_pending_audit_when_export_fails(self):
        for existing_corruption in (False, True):
            with self.subTest(existing_corruption=existing_corruption), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                seed(home)
                runtime, ctx = create_runtime(home)
                try:
                    ctx.target = home / "document.txt"
                    before = digest(ctx)
                    draft = model(ctx, "propose", request="block forbidden")["draft"]
                    manager = runtime.manager
                    managed = home / "guiderails" / "patterns" / "managed.yaml"
                    if existing_corruption:
                        manager.audit.path.write_bytes(b"corrupt\n")
                    flush = manager.audit.flush

                    def export():
                        if not existing_corruption and managed.exists():
                            raise OSError("private filesystem detail")
                        return flush()

                    with patch.object(manager.audit, "flush", side_effect=export):
                        result = command(ctx, f"approve {draft['draft_id']} {draft['digest']}")
                    self.assertEqual(result, {"ok": True, "ruleset_digest": manager.engine.digest,
                                              "audit_pending": True, "audit_error_code":
                                                  "corrupt_audit_json" if existing_corruption else "storage_error"})
                    self.assertNotEqual(manager.engine.digest, before)
                    self.assertEqual(manager.store.outbox_count(), 2)
                    if existing_corruption:
                        manager.audit.path.unlink()
                    self.assertEqual(inspect(ctx, "forbidden")["action"], "block")
                    self.assertEqual(manager.store.outbox_count(), 0)
                    self.assertEqual(sum(event["event_type"] == "pattern_changed" for event in audit_events(home)), 1)
                finally:
                    runtime.close()

    def test_proposal_approval_reload_and_profile_isolation(self):
        other = self.root / "profile-b"
        seed(other)
        other_runtime, other_ctx = create_runtime(other)
        self.addCleanup(other_runtime.close)
        other_ctx.target = other / "output.txt"
        before = digest(self.ctx)
        self.assertIsNone(inspect(self.ctx, "forbidden"))
        draft = self.propose()
        self.assertFalse(json.loads(draft["document"])["rules"][0]["enabled"])
        self.assertEqual(digest(self.ctx), before)
        self.assertIsNone(inspect(self.ctx, "forbidden"))

        bad = command(self.ctx, f"approve {draft['draft_id']} {'0' * 64}")
        self.assertFalse(bad["ok"])
        self.assertEqual(digest(self.ctx), before)
        self.assertFalse(model(self.ctx, "approve", draft_id=draft["draft_id"])["ok"])
        self.assertEqual(digest(self.ctx), before)
        approved = command(self.ctx, f"approve {draft['draft_id']} {draft['digest']}")
        self.assertTrue(approved["ok"], approved)
        after = digest(self.ctx)
        self.assertNotEqual(after, before)
        self.assertEqual(inspect(self.ctx, "forbidden")["message"].splitlines()[0],
                         "guiderails.pattern_detected")
        self.assertEqual(model(self.ctx, "test", rule_id="test.proposed",
                               sample="forbidden")["test"]["samples"], [True])
        self.assertEqual(digest(other_ctx), before)
        self.assertIsNone(inspect(other_ctx, "forbidden"))

        broken = self.home / "guiderails" / "patterns" / "broken.yaml"
        broken.write_text("schema_version: 2\nrules: []\n", encoding="utf-8")
        self.assertFalse(command(self.ctx, "reload")["ok"])
        self.assertEqual(digest(self.ctx), after)
        self.assertEqual(inspect(self.ctx, "forbidden", request="request-2")["action"], "block")
        broken.unlink()
        self.assertTrue(command(self.ctx, "reload")["ok"])
        self.assertEqual(digest(self.ctx), after)
        other_stats = model(other_ctx, "stats")["stats"]
        self.assertEqual(other_stats["detection_count"], 0)

    def test_detection_identity_and_private_audit(self):
        for request, session, call in [
            ("request-1", "session-1", "call-1"),
            ("request-1", "session-1", "call-1"),
            ("request-2", "session-1", "call-1"),
            ("request-3", "session-2", "call-2"),
        ]:
            self.assertEqual(inspect(self.ctx, "original private payload", request=request,
                                     session=session, call=call)["action"], "block")
        self.assertEqual(model(self.ctx, "stats")["stats"]["detection_count"], 3)
        self.assertEqual(model(self.ctx, "stats", session_id="session-1")["stats"]
                         ["detection_count"], 2)
        events = model(self.ctx, "report", session_id="session-1")["events"]
        detected = [event for event in events if event["event_type"] == "detected"]
        self.assertEqual(len(detected), 2)
        lines = audit_events(self.home)
        ids = [event["event_id"] for event in lines]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("private payload", json.dumps(lines))

    def test_detection_deduplication_survives_runtime_restart(self):
        self.assertEqual(inspect(self.ctx, "original")["action"], "block")
        self.assertEqual(model(self.ctx, "stats")["stats"]["detection_count"], 1)
        self.runtime.close()

        runtime, ctx = create_runtime(self.home)
        self.addCleanup(runtime.close)
        ctx.target = self.ctx.target
        self.assertEqual(inspect(ctx, "original")["action"], "block")
        self.assertEqual(model(ctx, "stats")["stats"]["detection_count"], 1)
        self.assertEqual(inspect(ctx, "original", request="request-2")["action"], "block")
        self.assertEqual(model(ctx, "stats")["stats"]["detection_count"], 2)
        detected = [event for event in model(ctx, "report")["events"]
                    if event["event_type"] == "detected"]
        self.assertEqual(len(detected), 2)
        ids = [event["event_id"] for event in audit_events(self.home)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_audit_append_before_ack_recovers_without_duplicate(self):
        manager = self.runtime.manager
        manager.store.db.execute(
            "CREATE TRIGGER fail_export BEFORE UPDATE OF exported ON events "
            "BEGIN SELECT RAISE(FAIL, 'private fault'); END"
        )
        try:
            blocked = inspect(self.ctx, "original")
            self.assertEqual(blocked["action"], "block")
            self.assertNotIn("private fault", blocked["message"])
            pending = manager.store.pending_events()
            self.assertEqual(len(pending), 1)
            event_id = pending[0]["event_id"]
            self.assertEqual([event["event_id"] for event in audit_events(self.home)].count(event_id), 1)
        finally:
            manager.store.db.execute("DROP TRIGGER fail_export")
        self.assertTrue(model(self.ctx, "status")["ok"])
        self.assertEqual(manager.store.outbox_count(), 0)
        self.assertEqual([event["event_id"] for event in audit_events(self.home)].count(event_id), 1)

    def test_corrupt_audit_preserves_pending_then_repairs_partial_tail(self):
        manager = self.runtime.manager
        path = self.home / "guiderails" / "audit" / "events.jsonl"
        corrupt = b'{"event_id": "private incomplete record"}\n'
        path.write_bytes(corrupt)
        blocked = inspect(self.ctx, "original")
        self.assertEqual(blocked["action"], "block")
        self.assertEqual(path.read_bytes(), corrupt)
        pending = manager.store.pending_events()
        self.assertEqual(len(pending), 1)
        event_id = pending[0]["event_id"]

        path.write_bytes(b'{"incomplete":')
        self.assertTrue(model(self.ctx, "status")["ok"])
        self.assertEqual(manager.store.outbox_count(), 0)
        events = audit_events(self.home)
        self.assertEqual([event["event_id"] for event in events], [event_id])
        self.assertNotIn("private incomplete", path.read_text(encoding="utf-8"))

    def test_detection_recovers_full_outbox_after_export_ack_is_restored(self):
        (self.home / "guiderails" / "config.yaml").write_text(
            "schema_version: 1\nmax_rejections: 1\nlogging:\n  max_outbox: 2\n", encoding="utf-8")
        manager = self.runtime.manager
        manager.store.db.execute(
            "CREATE TRIGGER fail_export BEFORE UPDATE OF exported ON events "
            "BEGIN SELECT RAISE(FAIL, 'private fault'); END"
        )
        self.assertIn("guiderails.pattern_detected", inspect(self.ctx, "original", call="first")["message"])
        self.assertIsNone(inspect(self.ctx, "original", call="second"))
        self.assertIsNone(inspect(self.ctx, "original", session="fresh-session", call="while-full"))
        pending_ids = [row["event_id"] for row in manager.store.pending_events()]
        written = [event["event_id"] for event in audit_events(self.home)]
        self.assertEqual(len(pending_ids), 2)
        self.assertEqual(written, pending_ids)
        self.assertEqual(manager.store.stats()["blocked_attempt_count"], 1)
        manager.store.db.execute("DROP TRIGGER fail_export")
        self.runtime.close()

        runtime, ctx = create_runtime(self.home)
        self.addCleanup(runtime.close)
        ctx.target = self.ctx.target
        recovered = inspect(ctx, "original", session="fresh-session", call="recovered")
        self.assertIn("guiderails.pattern_detected", recovered["message"])
        self.assertEqual(runtime.manager.store.outbox_count(), 0)
        events = audit_events(self.home)
        self.assertEqual([event["unit_id"] for event in events], ["first", "second", "recovered"])
        self.assertEqual([event["event_id"] for event in events[:2]], pending_ids)
        self.assertEqual(len({event["event_id"] for event in events}), 3)
        self.assertIsNone(inspect(ctx, "original", session="fresh-session", call="over-budget"))

    def test_file_and_commit_failures_preserve_active_policy(self):
        draft = self.propose()
        before = digest(self.ctx)
        target = self.home / "guiderails" / "patterns" / "managed.yaml"
        import guiderails.manager as manager_module
        atomic_write = manager_module.atomic_write

        def fail_replacement(path, content):
            if path == target:
                raise OSError("private file failure")
            return atomic_write(path, content)

        with patch.object(manager_module, "atomic_write", side_effect=fail_replacement):
            result = command(self.ctx, f"approve {draft['draft_id']} {draft['digest']}")
        self.assertFalse(result["ok"])
        self.assertNotIn("private file failure", json.dumps(result))
        self.assertFalse(target.exists())
        self.assertEqual(digest(self.ctx), before)
        self.assertIsNone(inspect(self.ctx, "forbidden"))

        # A deferred FK violation fails at COMMIT after the managed file was replaced.
        db = self.runtime.manager.store.db
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE fault_parent (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE fault_child (id INTEGER REFERENCES fault_parent(id) "
                   "DEFERRABLE INITIALLY DEFERRED)")
        db.execute("CREATE TRIGGER fail_commit AFTER INSERT ON events "
                   "WHEN json_extract(NEW.payload, '$.event_type') = 'pattern_changed' "
                   "BEGIN INSERT INTO fault_child VALUES (1); END")
        try:
            result = command(self.ctx, f"approve {draft['draft_id']} {draft['digest']}")
        finally:
            db.execute("DROP TRIGGER fail_commit")
        self.assertFalse(result["ok"])
        self.assertFalse(target.exists())
        self.assertEqual(digest(self.ctx), before)
        self.assertIsNone(inspect(self.ctx, "forbidden"))
        self.assertTrue(command(self.ctx, f"approve {draft['draft_id']} {draft['digest']}")["ok"])

    def test_interrupted_replace_is_reconciled_on_restart(self):
        draft = self.propose()
        before = digest(self.ctx)
        self.runtime.close()
        # Kill only the isolated child, after real replacement and before SQLite COMMIT.
        script = r"""
import os, sys
from pathlib import Path
from integration.test_profile_integration import create_runtime, command
import guiderails.manager as manager
home = Path(sys.argv[1])
runtime, context = create_runtime(home)
original = manager.atomic_write
def crash(path, content):
    original(path, content)
    if path.name == 'managed.yaml':
        os._exit(77)
manager.atomic_write = crash
command(context, 'approve ' + sys.argv[2] + ' ' + sys.argv[3])
os._exit(78)
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.home), draft["draft_id"], draft["digest"]],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(child.returncode, 77, child.stderr)
        self.assertTrue((self.home / "guiderails" / "patterns" / "managed.yaml").exists())
        runtime, ctx = create_runtime(self.home)
        self.addCleanup(runtime.close)
        ctx.target = self.ctx.target
        after = digest(ctx)
        self.assertNotEqual(after, before)
        self.assertEqual(inspect(ctx, "forbidden")["action"], "block")
        events = model(ctx, "report")["events"]
        changes = [event for event in events if event["event_type"] == "pattern_changed"
                   and event["ruleset_digest"] == after]
        self.assertEqual(len(changes), 1)
        self.assertTrue(model(ctx, "status")["ok"])
        ids = [event["event_id"] for event in audit_events(self.home)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_provider_wait_does_not_block_inspection_or_unload(self):
        entered, release = threading.Event(), threading.Event()
        complete = self.ctx.complete

        def provider(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("provider fixture timed out")
            return complete(**kwargs)

        self.ctx.llm.complete_structured = provider
        before = digest(self.ctx)
        drafts = self.home / "guiderails" / "drafts"
        with ThreadPoolExecutor(max_workers=3) as pool:
            proposal = pool.submit(model, self.ctx, "propose", request="Reject forbidden.")
            try:
                self.assertTrue(entered.wait(5))
                hook = pool.submit(inspect, self.ctx, "original")
                self.assertEqual(hook.result(timeout=3)["action"], "block")
                unload = pool.submit(self.ctx.unload[0])
                self.assertIsNone(unload.result(timeout=3))
                self.assertFalse(proposal.done())
            finally:
                release.set()
            result = proposal.result(timeout=5)
        self.assertEqual(result["error_code"], "proposal_invalidated")
        self.assertEqual(list(drafts.glob("*.json")), [])
        self.assertEqual(model(self.ctx, "status")["error_code"], "runtime_closed")
        self.assertIsNone(inspect(self.ctx, "original"))
        self.assertIsNone(self.runtime._manager)
        self.assertTrue(before)

    def test_scope_change_invalidates_delayed_proposal_even_if_restored(self):
        entered, release = threading.Event(), threading.Event()
        complete = self.ctx.complete

        def provider(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("provider fixture timed out")
            return complete(**kwargs)

        self.ctx.llm.complete_structured = provider
        before = digest(self.ctx)
        with ThreadPoolExecutor(max_workers=2) as pool:
            proposal = pool.submit(model, self.ctx, "propose", request="Reject forbidden.")
            try:
                self.assertTrue(entered.wait(5))
                self.ctx.profile_name = "other"
                self.assertIsNone(pool.submit(inspect, self.ctx, "original").result(timeout=3))
                self.ctx.profile_name = "fixture"
            finally:
                release.set()
            self.assertEqual(proposal.result(timeout=5)["error_code"], "proposal_invalidated")
        self.assertEqual(digest(self.ctx), before)
        self.assertEqual(list((self.home / "guiderails" / "drafts").glob("*.json")), [])

    def test_home_change_invalidates_delayed_proposal(self):
        home = self.root / "profile-home-change"
        seed(home)
        current = [home]
        runtime, ctx = create_runtime(home, current_home=lambda: current[0])
        self.addCleanup(runtime.close)
        ctx.target = home / "output.txt"
        before = digest(ctx)
        entered, release = threading.Event(), threading.Event()
        complete = ctx.complete

        def provider(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("provider fixture timed out")
            return complete(**kwargs)

        ctx.llm.complete_structured = provider
        with ThreadPoolExecutor(max_workers=2) as pool:
            proposal = pool.submit(model, ctx, "propose", request="Reject forbidden.")
            try:
                self.assertTrue(entered.wait(5))
                current[0] = self.root / "unrelated-profile"
                self.assertIsNone(pool.submit(inspect, ctx, "original").result(timeout=3))
                current[0] = home
            finally:
                release.set()
            self.assertEqual(proposal.result(timeout=5)["error_code"], "proposal_invalidated")
        self.assertEqual(digest(ctx), before)
        self.assertEqual(list((home / "guiderails" / "drafts").glob("*.json")), [])
        self.assertFalse((self.root / "unrelated-profile").exists())

    def test_provider_failure_is_private_and_does_not_create_draft(self):
        before = digest(self.ctx)

        def fail(**kwargs):
            raise RuntimeError("private provider body")

        self.ctx.llm.complete_structured = fail
        result = model(self.ctx, "propose", request="Reject forbidden.")
        self.assertEqual(result["error_code"], "pattern_provider_failed")
        self.assertNotIn("private provider body", json.dumps(result))
        self.assertEqual(digest(self.ctx), before)
        self.assertEqual(list((self.home / "guiderails" / "drafts").glob("*.json")), [])

    def test_concurrent_proposals_remain_inactive(self):
        barrier, release = threading.Barrier(3), threading.Event()
        complete = self.ctx.complete

        def provider(**kwargs):
            barrier.wait(timeout=5)
            if not release.wait(5):
                raise TimeoutError("provider fixture timed out")
            return complete(**kwargs)

        self.ctx.llm.complete_structured = provider
        before = digest(self.ctx)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(model, self.ctx, "propose", request="Reject forbidden.")
                       for _ in range(2)]
            try:
                barrier.wait(timeout=5)
                self.assertEqual(digest(self.ctx), before)
            finally:
                release.set()
            results = [future.result(timeout=5) for future in futures]
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(len({result["draft"]["draft_id"] for result in results}), 2)
        self.assertTrue(all(not json.loads(result["draft"]["document"])["rules"][0]["enabled"]
                            for result in results))
        self.assertEqual(len(list((self.home / "guiderails" / "drafts").glob("*.json"))), 2)
        self.assertEqual(digest(self.ctx), before)
        self.assertIsNone(inspect(self.ctx, "forbidden"))


if __name__ == "__main__":
    unittest.main()
