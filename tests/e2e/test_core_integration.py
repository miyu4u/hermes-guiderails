"""Native discovery and real Hermes tool execution with deterministic offline responses."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


CORE_AVAILABLE = importlib.util.find_spec("hermes_cli") is not None


def rule(*, matcher=None, scopes=None, tests=None):
    return {
        "id": "fixture.forbidden", "revision": 1, "enabled": True,
        "description": "Fixture rule", "priority": 100,
        "scopes": scopes or ["tool.arguments", "artifact.text"],
        "matcher": matcher or {"type": "match", "value": "금지", "case_sensitive": True,
                               "normalization": "nfc"},
        "edit_policy": "whole_result",
        "announce": {"instruction": "허용된 문장으로 다시 작성하세요."},
        "tests": tests or {"positive": ["금지"], "negative": ["정상"]},
    }


def document(entry=None):
    return json.dumps({"schema_version": 1, "rules": [entry or rule()]}, ensure_ascii=False)


class EventStream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.events)

    def close(self):
        self.closed = True


def text_events(*chunks):
    return [
        SimpleNamespace(type="response.output_item.added",
                        item=SimpleNamespace(type="message", role="assistant", id="message", phase=None)),
        *[SimpleNamespace(type="response.output_text.delta", item_id="message", delta=chunk) for chunk in chunks],
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            id="response", status="completed", usage=None, output=[], incomplete_details=None, error=None)),
    ]


def tool_events(name, arguments, call_id):
    return batch_events((name, arguments, call_id))


def batch_events(*calls):
    events = []
    for index, (name, arguments, call_id) in enumerate(calls):
        item = SimpleNamespace(type="function_call", id=call_id, call_id=call_id,
                               name=name, arguments=json.dumps(arguments, ensure_ascii=False))
        events.extend([SimpleNamespace(type="response.output_item.added", output_index=index, item=item),
                       SimpleNamespace(type="response.output_item.done", output_index=index, item=item)])
    return [*events, text_events()[-1]]


@unittest.skipUnless(CORE_AVAILABLE, "requires Hermes core and its committed dependencies")
class CoreIntegrationTests(unittest.TestCase):
    @contextmanager
    def fixture(self, settings="", *, patterns=None):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.plugins import PluginManager
        from tools import file_tools  # Register native file handlers independently of test order.

        sandbox = os.environ.get("HERMES_TEST_SANDBOX_HOME")
        if not sandbox or os.environ.get("HERMES_HOME") != sandbox or os.environ.get("HOME") != sandbox:
            self.fail("native integration requires the isolated tests/run.py environment")
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            (home / "home").mkdir()
            source = Path(__file__).resolve().parents[2]
            target = home / "plugins/guiderails"
            target.mkdir(parents=True)
            for name in ("__init__.py", "plugin.yaml"):
                shutil.copy2(source / name, target / name)
            shutil.copytree(source / "src", target / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            (home / "config.yaml").write_text(
                "plugins:\n  isolation: in_process\n  enabled: [guiderails]\nterminal:\n  backend: local\n",
                encoding="utf-8")
            root = home / "guiderails"
            (root / "patterns").mkdir(parents=True)
            (root / "patterns/fixture.yaml").write_text(patterns or document(), encoding="utf-8")
            (root / "config.yaml").write_text("schema_version: 1\n" + settings, encoding="utf-8")
            token = set_hermes_home_override(home)
            manager = PluginManager(scope_key=str(home))
            try:
                with patch("hermes_cli.plugins.get_bundled_plugins_dir", return_value=home / "empty-bundled"), \
                     patch.object(manager, "_scan_entry_points", return_value=[]):
                    manager.discover_and_load()
                callbacks = tuple(manager.iter_hook_callbacks("pre_tool_call"))
                with patch("hermes_cli.plugins._delivery_manager", return_value=manager):
                    yield home, manager, callbacks[0].__self__
            finally:
                manager.unload()
                reset_hermes_home_override(token)

    @contextmanager
    def agent(self, streams, on_request=None):
        from run_agent import AIAgent

        requests = []

        def create(**kwargs):
            extra = kwargs.pop("extra_body", {})
            requests.append(deepcopy(dict(kwargs, **extra)))
            if on_request is not None:
                on_request(len(requests))
            return streams[len(requests) - 1]

        client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
        with patch("model_tools.get_tool_definitions", return_value=[]), \
             patch("model_tools.check_toolset_requirements", return_value={}):
            agent = AIAgent(model="fixture-model", provider="openai-codex", api_mode="codex_responses",
                            base_url="https://fixture.invalid", api_key="fixture-token", quiet_mode=True,
                            max_iterations=12, skip_context_files=True, skip_memory=True)
            agent._create_request_openai_client = lambda *args, **kwargs: client
            agent._close_request_openai_client = lambda *args, **kwargs: None
            agent._cleanup_task_resources = lambda task_id: None
            agent._persist_session = lambda messages, history=None: None
            agent._save_trajectory = lambda *args, **kwargs: None
            yield agent, requests

    def test_response_stream_is_not_inspected(self):
        with self.fixture() as (_, _, runtime):
            stream = EventStream(text_events("앞 ", "금지", " 뒤"))
            with self.agent([stream]) as (agent, requests):
                delivered = []
                agent.stream_delta_callback = delivered.append
                result = agent.run_conversation("간단히 답하세요")
            self.assertTrue(result["completed"], result)
            self.assertEqual(result["final_response"], "앞 금지 뒤")
            self.assertEqual("".join(delivered), "앞 금지 뒤")
            self.assertEqual(len(requests), 1)
            self.assertTrue(stream.closed)
            self.assertEqual(runtime.manager.store.stats()["detection_count"], 0)

    def test_rejection_budget_across_real_turns_and_requests(self):
        from tools.registry import registry

        for configured, limit in ((None, 3), (0, 0), (1, 1), (3, 3)):
            settings = "" if configured is None else f"max_rejections: {configured}\n"
            with self.subTest(configured=configured), self.fixture(settings) as (home, _, runtime):
                targets = [home / f"turn-{index}.txt" for index in range(4)]
                streams = [stream for index, target in enumerate(targets)
                           for stream in (EventStream(tool_events("write_file", {
                               "path": str(target), "content": f"금지 {index}"}, f"request-{index}")),
                                          EventStream(text_events("완료")))]
                entry = registry.get_entry("write_file")
                with self.agent(streams) as (agent, requests), \
                     patch.object(entry, "handler", wraps=entry.handler) as handler:
                    agent.valid_tool_names = {"write_file"}
                    session_id = agent.session_id
                    history = None
                    for index in range(4):
                        result = agent.run_conversation(f"쓰기 {index}", conversation_history=history)
                        self.assertTrue(result["completed"], result)
                        self.assertEqual(agent.session_id, session_id)
                        history = result["messages"]
                    self.assertEqual(handler.call_count, 4 - limit)
                self.assertEqual(len(requests), 8)
                events = runtime.manager.store.report(session_id=session_id)
                self.assertEqual([row["decision"] for row in events],
                                 ["blocked"] * limit + ["warned_allowed"] * (4 - limit))
                self.assertEqual(runtime.manager.store.stats(session_id=session_id)["blocked_attempt_count"], limit)
                for index, target in enumerate(targets):
                    first, followup = requests[2 * index:2 * index + 2]
                    self.assertNotIn("<guiderails>", first["instructions"])
                    output, = [row["output"] for row in followup["input"]
                               if row.get("type") == "function_call_output"
                               and row.get("call_id") == f"request-{index}"]
                    if index < limit:
                        self.assertFalse(target.exists())
                        self.assertIn("guiderails.pattern_detected", output)
                        self.assertIn(
                            "<guiderails>\nGuiderails correction (fixture.forbidden): "
                            "The call was blocked because it violated a rule.\n"
                            "Rule: Fixture rule\nHow to fix it: 허용된 문장으로 다시 작성하세요.",
                            followup["instructions"],
                        )
                        self.assertTrue(followup["instructions"].endswith("\n</guiderails>"))
                    else:
                        self.assertEqual(target.read_text(), f"금지 {index}")
                        self.assertNotIn("guiderails.pattern_detected", output)
                        self.assertIn(target.name, output)
                        self.assertIn(
                            "<guiderails>\nGuiderails warning (fixture.forbidden): "
                            "The call was allowed because the rejection limit was reached.\n"
                            "Rule: Fixture rule\nHow to fix the next call: 허용된 문장으로 다시 작성하세요.\n"
                            "</guiderails>",
                            followup["instructions"],
                        )

    def test_native_patch_argument_rule_allows_removal_and_blocks_reintroduction(self):
        from tools.registry import registry

        entry = dict(rule(scopes=["tool.arguments"]), argument={"tool": "patch", "path": "/new_string"})
        with self.fixture(patterns=document(entry)) as (home, _, runtime):
            target = home / "patch.txt"
            target.write_text("금지", encoding="utf-8")
            calls = [
                ("patch", {"path": str(target), "old_string": "금지", "new_string": "정상"}, "remove"),
                ("patch", {"path": str(target), "old_string": "정상", "new_string": "금지"}, "introduce"),
            ]
            streams = [*(EventStream(tool_events(*call)) for call in calls), EventStream(text_events("완료"))]
            handler = registry.get_entry("patch")
            with self.agent(streams) as (agent, requests), \
                 patch.object(handler, "handler", wraps=handler.handler) as executed:
                agent.valid_tool_names = {"patch"}
                result = agent.run_conversation("파일을 수정하세요.")
                self.assertTrue(result["completed"], result)
                self.assertEqual(executed.call_count, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "정상")
            self.assertIn("guiderails.pattern_detected", str(requests[2]["input"]))
            event, = runtime.manager.store.report()
            self.assertEqual([(hit["field_path"], hit["start"]) for hit in event["locations"]],
                             [("/new_string", 0)])

    @contextmanager
    def notion_handlers(self):
        from tools.mcp_tool_schema import mcp_prefixed_tool_name
        from tools.registry import registry

        names = tuple(mcp_prefixed_tool_name("notion", tool)
                      for tool in ("notion-create-pages", "notion-update-page"))
        calls = {name: [] for name in names}
        for name in names:
            self.assertIsNone(registry.get_entry(name), name)

        def handler(name):
            def execute(args):
                calls[name].append(deepcopy(args))
                return json.dumps({"fixture_result": name, "executed": len(calls[name])})
            return execute

        try:
            for name in names:
                registry.register(name=name, toolset="mcp-fixture",
                                  schema={"name": name, "parameters": {"type": "object"}},
                                  handler=handler(name))
            yield names, calls
        finally:
            for name in names:
                registry.deregister(name)

    def test_native_notion_selectors_share_budget_and_preserve_corrective_edit(self):
        pattern = (Path(__file__).resolve().parents[2] /
                   "examples/patterns/ko.avoid-meaning-disclaimer.yaml").read_text(encoding="utf-8")
        bad = "검토는 끝났다. 공식 승인을 뜻하지 않는다."
        clean = "검토는 끝났다. 공식 승인은 금요일에 진행한다."
        create_args = {"creation_mode": "draft", "pages": [
            {"properties": {"title": "첫째"}, "content": clean},
            {"properties": {"title": "둘째"}, "content": bad}]}
        edit_args = {"page_id": "fixture-page", "command": "update_content", "content_updates": [
            {"old_str": "첫째", "new_str": clean}, {"old_str": "둘째", "new_str": bad}]}
        correction = {"page_id": "fixture-page", "command": "update_content", "content_updates": [
            {"old_str": bad, "new_str": clean}]}
        warned = {"page_id": "fixture-page", "command": "replace_content", "new_str": bad}
        with self.fixture("max_rejections: 2\n", patterns=pattern) as (_, _, runtime), \
             self.notion_handlers() as (names, executed):
            create, update = names
            calls = [(create, create_args, "create-blocked"), (update, edit_args, "edit-blocked"),
                     (update, correction, "correction"), (update, warned, "warning")]
            streams = [*(EventStream(tool_events(*call)) for call in calls),
                       EventStream(text_events("완료"))]

            def check_before_next_request(number):
                if number == 2:
                    self.assertEqual(executed[create], [])
                elif number == 3:
                    self.assertEqual(executed[update], [])
                elif number == 4:
                    self.assertEqual(executed[update], [correction])

            with self.agent(streams, on_request=check_before_next_request) as (agent, requests):
                agent.valid_tool_names = set(names)
                session_id = agent.session_id
                result = agent.run_conversation("Notion 문서를 작성하고 교정하세요.")
            self.assertTrue(result["completed"], result)
            self.assertEqual(executed[create], [])
            self.assertEqual(executed[update], [correction, warned])
            self.assertEqual(len(requests), 5)
            for index, call_id in ((1, "create-blocked"), (2, "edit-blocked")):
                output, = [item["output"] for item in requests[index]["input"]
                           if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
                self.assertIn("guiderails.pattern_detected", output)
            for index, call_id in ((3, "correction"), (4, "warning")):
                output, = [item["output"] for item in requests[index]["input"]
                           if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
                self.assertIn(json.dumps({"fixture_result": update, "executed": index - 2}), output)
            self.assertIn("Guiderails warning", requests[4]["instructions"])
            self.assertEqual(runtime.manager.store.stats(session_id=session_id)["blocked_attempt_count"], 2)
            runtime.manager.audit.flush()
            events = [json.loads(line) for line in runtime.manager.audit.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["decision"] for event in events], ["blocked", "blocked", "warned_allowed"])
            self.assertEqual([event["session_id"] for event in events], [session_id] * 3)
            self.assertEqual([[location["field_path"] for location in event["locations"]] for event in events],
                             [["/pages/1/content"], ["/content_updates/1/new_str"], ["/new_str"]])
            self.assertTrue(all(stream.closed for stream in streams))

    def test_native_notion_insertion_and_replacement_are_inspected(self):
        pattern = (Path(__file__).resolve().parents[2] /
                   "examples/patterns/ko.avoid-meaning-disclaimer.yaml").read_text(encoding="utf-8")
        bad = "검토는 끝났다. 공식 승인을 뜻하지 않는다."
        for field in ("content", "new_str"):
            with self.subTest(field=field), self.fixture(patterns=pattern) as (_, _, runtime), \
                 self.notion_handlers() as (names, executed):
                _, update = names
                command = "insert_content" if field == "content" else "replace_content"
                streams = [EventStream(tool_events(update, {"page_id": "fixture-page", "command": command,
                                                            field: bad}, field)),
                           EventStream(text_events("완료"))]
                with self.agent(streams) as (agent, requests):
                    agent.valid_tool_names = set(names)
                    result = agent.run_conversation("Notion 본문을 수정하세요.")
                self.assertTrue(result["completed"], result)
                self.assertEqual(executed[update], [])
                self.assertIn("guiderails.pattern_detected", str(requests[1]["input"]))
                runtime.manager.audit.flush()
                event, = [json.loads(line) for line in runtime.manager.audit.path.read_text(
                    encoding="utf-8").splitlines()]
                self.assertEqual(event["decision"], "blocked")
                self.assertEqual([hit["field_path"] for hit in event["locations"]], [f"/{field}"])
                self.assertTrue(all(stream.closed for stream in streams))

    def test_keep_controls_real_batched_tool_pairs_only(self):
        from tools.registry import registry

        for keep in (True, False):
            setting = dict(rule(), keep=keep)
            with self.subTest(keep=keep), self.fixture(patterns=document(setting)) as (home, _, runtime):
                rejected = [home / f"rejected-{index}.txt" for index in range(2)]
                accepted = home / "accepted.txt"
                calls = [("write_file", {"path": str(path), "content": "금지"}, path.stem) for path in rejected]
                calls.insert(1, ("write_file", {"path": str(accepted), "content": "정상"}, "accepted"))
                streams = [EventStream(batch_events(*calls)), EventStream(text_events("완료"))]
                entry = registry.get_entry("write_file")
                published = threading.Event()
                notifications = []

                def post(text):
                    notifications.append(text)
                    if len(notifications) == 2:
                        published.set()
                    return SimpleNamespace(result={"ok": True})

                with self.agent(streams) as (agent, requests), \
                     patch.object(runtime.ctx, "post_message", side_effect=post), \
                     patch.object(entry, "handler", wraps=entry.handler) as handler:
                    agent.valid_tool_names = {"write_file"}
                    result = agent.run_conversation("파일 세 개를 작성하세요.")
                    self.assertTrue(result["completed"], result)
                    self.assertEqual(handler.call_count, 1)
                    self.assertTrue(published.wait(2), notifications)
                self.assertEqual(len(requests), 2)
                self.assertFalse(any(path.exists() for path in rejected))
                self.assertEqual(accepted.read_text(), "정상")
                followup = requests[1]
                self.assertEqual(followup["instructions"].count(setting["announce"]["instruction"]), 1)
                self.assertNotIn("discard-tool-result", str(followup))
                expected = [path.stem for path in rejected] + ["accepted"] if keep else ["accepted"]
                for kind in ("function_call", "function_call_output"):
                    self.assertCountEqual([item["call_id"] for item in followup["input"] if item.get("type") == kind],
                                          expected)
                outcomes = {item["call_id"]: item["output"] for item in followup["input"]
                            if item.get("type") == "function_call_output"}
                self.assertIn("accepted.txt", outcomes["accepted"])
                self.assertEqual(sum("guiderails.pattern_detected" in value for value in outcomes.values()),
                                 2 if keep else 0)
                events = runtime.manager.store.report()
                self.assertEqual([row["decision"] for row in events], ["blocked", "blocked"])
                self.assertEqual(len(notifications), 2)
                for event in events:
                    self.assertTrue(any(
                        text == f"Guiderails · BLOCKED · write_file · {event['event_id'][:8]}"
                        for text in notifications
                    ), notifications)

    def test_blocked_write_is_repaired_by_agent_without_creating_forbidden_file(self):
        from tools import file_tools
        from tools.registry import registry

        with self.fixture() as (home, _, runtime):
            target = home / "new-document.txt"
            streams = [
                EventStream(tool_events("write_file", {"path": str(target), "content": "금지"}, "blocked-write")),
                EventStream(tool_events("write_file", {"path": str(target), "content": "정상"}, "repaired-write")),
                EventStream(text_events("작성했습니다.")),
            ]
            entry = registry.get_entry("write_file")
            notified = threading.Event()
            notifications = []

            def post(text):
                notifications.append(text)
                notified.set()
                return SimpleNamespace(result={"ok": True})

            def check_file_before_repair(request_number):
                if request_number == 2:
                    self.assertFalse(target.exists())

            with self.agent(streams, on_request=check_file_before_repair) as (agent, requests), \
                 patch.object(runtime.ctx, "post_message", side_effect=post), \
                 patch.object(entry, "handler", wraps=entry.handler) as handler, \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes:
                agent.valid_tool_names = {"write_file"}
                result = agent.run_conversation("파일을 작성하세요")
                self.assertTrue(notified.wait(2))
                event, = runtime.manager.store.report()
                self.assertEqual(notifications, [
                    f"Guiderails · BLOCKED · write_file · {event['event_id'][:8]}",
                ])
            self.assertTrue(result["completed"], result)
            self.assertEqual(target.read_bytes(), "정상".encode())
            self.assertEqual(handler.call_count, 1)
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(writes.call_args.kwargs["content"], "정상")
            self.assertEqual(len(requests), 3)
            self.assertIn("guiderails.pattern_detected", str(requests[1]["input"]))
            self.assertIn("blocked-write", str(requests[1]["input"]))
            self.assertIn("repaired-write", str(requests[2]["input"]))
            self.assertEqual(sum(row.get("role") == "user" for row in result["messages"]), 1)
            self.assertEqual(result["final_response"], "작성했습니다.")
            self.assertEqual(runtime.manager.store.stats()["detection_count"], 1)
            self.assertTrue(all(stream.closed for stream in streams))

    def test_prior_write_read_and_rule_repair_share_one_turn(self):
        from tools import file_tools
        from tools.registry import registry

        with self.fixture() as (home, _, runtime):
            first, target = home / "first.txt", home / "document.txt"
            target.write_text("금 중간 지", encoding="utf-8")
            streams = [
                EventStream(tool_events("write_file", {"path": str(first), "content": "처리 완료"}, "first")),
                EventStream(tool_events("read_file", {"path": str(target)}, "read")),
                EventStream(tool_events("patch", {"path": str(target), "old_string": " 중간 ",
                                                 "new_string": ""}, "blocked")),
                EventStream(tool_events("patch", {"path": str(target), "old_string": " 중간 ",
                                                 "new_string": " 사이 "}, "corrected")),
                EventStream(text_events("교정했습니다.")),
            ]
            write_entry = registry.get_entry("write_file")
            read_entry = registry.get_entry("read_file")
            edit_entry = registry.get_entry("patch")
            with self.agent(streams) as (agent, requests), \
                 patch.object(write_entry, "handler", wraps=write_entry.handler) as write_handler, \
                 patch.object(read_entry, "handler", wraps=read_entry.handler) as read_handler, \
                 patch.object(edit_entry, "handler", wraps=edit_entry.handler) as edit_handler, \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes, \
                 patch.object(file_tools, "read_file_tool", wraps=file_tools.read_file_tool) as reads, \
                 patch.object(file_tools, "patch_tool", wraps=file_tools.patch_tool) as edits:
                agent.valid_tool_names = {"write_file", "read_file", "patch"}
                result = agent.run_conversation("두 파일을 처리하세요")
            self.assertTrue(result["completed"], result)
            self.assertEqual(first.read_bytes(), "처리 완료".encode())
            self.assertEqual(target.read_bytes(), "금 사이 지".encode())
            self.assertEqual((writes.call_count, reads.call_count, edits.call_count), (1, 1, 1))
            self.assertEqual((write_handler.call_count, read_handler.call_count, edit_handler.call_count), (1, 1, 1))
            self.assertEqual(edits.call_args.kwargs["new_string"], " 사이 ")
            self.assertEqual(len(requests), 5)
            self.assertIn("금 중간 지", str(requests[2]["input"]))
            self.assertIn("guiderails.pattern_detected", str(requests[3]["input"]))
            self.assertEqual(sum(row.get("role") == "user" for row in result["messages"]), 1)
            self.assertEqual(result["final_response"], "교정했습니다.")
            self.assertEqual(runtime.manager.store.stats()["detection_count"], 1)
            self.assertTrue(all(stream.closed for stream in streams))

    def test_observe_allows_native_write_after_detection_or_storage_failure(self):
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools import file_tools
        from tools.registry import registry

        with self.fixture("mode: observe\n") as (home, _, runtime):
            target = home / "observe.txt"
            streams = [EventStream(tool_events("write_file", {"path": str(target), "content": "금지"}, "observed")),
                       EventStream(text_events("완료"))]
            with self.agent(streams) as (agent, requests), \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes:
                agent.valid_tool_names = {"write_file"}
                result = agent.run_conversation("파일을 작성하세요")
            self.assertTrue(result["completed"], result)
            self.assertEqual(len(requests), 2)
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(target.read_bytes(), "금지".encode())
            stats = json.loads(runtime.model_tool({"action": "stats"}))["stats"]
            self.assertEqual(stats["detection_count"], 1)
            self.assertEqual(stats["blocked_attempt_count"], 0)

            with patch.object(registry, "dispatch", wraps=registry.dispatch) as dispatch:
                message, _ = _dispatch_pre_tool_call_hooks(
                    "tool_call", {"calls": [{"name": "write_file",
                                             "arguments": {"path": str(target), "content": "금지"}}, None]})
                self.assertIsNotNone(message)
                self.assertIn("invalid_nested_calls", message)
                self.assertNotIn("금지", message)
                dispatch.assert_not_called()

            failed = home / "storage-failure.txt"
            streams = [EventStream(tool_events("write_file", {"path": str(failed), "content": "금지"}, "failed")),
                       EventStream(text_events("완료"))]
            with patch.object(runtime.manager.store, "record_detection", side_effect=OSError("private_body")), \
                 self.agent(streams) as (agent, requests), \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes:
                agent.valid_tool_names = {"write_file"}
                result = agent.run_conversation("파일을 작성하세요")
            self.assertTrue(result["completed"], result)
            self.assertEqual(len(requests), 2)
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(failed.read_bytes(), "금지".encode())
            self.assertEqual(result["final_response"], "완료")
            self.assertNotIn("private_body", str(requests))
            self.assertNotIn("private_body", str(result))

    def test_native_discovery_and_registration(self):
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command
        from hermes_cli.plugins import get_plugin_command_handler
        from model_tools import get_tool_definitions
        from tools.registry import registry

        with self.fixture() as (_, manager, _):
            callbacks = tuple(manager.iter_hook_callbacks("pre_tool_call"))
            self.assertEqual(len(callbacks), 1, "native discovery must register exactly one gate")
            self.assertIn("guiderails", manager._plugin_commands)
            self.assertIn("guiderails_patterns", manager._plugin_tool_names)
            names = {tool["function"]["name"] for tool in
                     get_tool_definitions(["guiderails"], skip_tool_search_assembly=True)}
            self.assertIn("guiderails_patterns", names)
            status = asyncio.run(invoke_plugin_command(get_plugin_command_handler("guiderails"), "status"))
            self.assertTrue(json.loads(status)["ok"], status)
            response = json.loads(registry.dispatch(
                "guiderails_patterns", {"action": "status"}, scope=manager.scope_key))
            self.assertTrue(response["ok"])

    def test_native_excerpt_is_redacted_expires_and_stays_out_of_jsonl(self):
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.registry import registry

        with self.fixture("logging:\n  excerpts: true\n") as (home, manager, runtime):
            def action(name):
                return json.loads(registry.dispatch("guiderails_patterns", {"action": name}, scope=manager.scope_key))

            target = home / "excerpt.txt"
            secret = "local-fixture-opaque-password-12345"
            content = f"금지\nPASSWORD={secret}\n"
            now = 1_700_000_000
            with patch("guiderails.state.time.time", return_value=now):
                message, _ = _dispatch_pre_tool_call_hooks(
                    "write_file", {"path": str(target), "content": content}, tool_call_id="excerpt")
            self.assertIn("guiderails.pattern_detected", message)
            self.assertNotIn(secret, message)
            expires, = runtime.manager.store.db.execute("SELECT expires FROM excerpts").fetchone()
            self.assertEqual(expires, now + 86400)
            with patch("guiderails.state.time.time", return_value=expires - 0.001):
                event, = action("report")["events"]
            self.assertIn("금지", event["excerpt"])
            self.assertNotIn(secret, event["excerpt"])
            audit, = [json.loads(line) for line in runtime.manager.audit.path.read_text().splitlines()]
            self.assertNotIn("excerpt", audit)
            self.assertNotIn(secret, json.dumps(audit))
            with patch("guiderails.state.time.time", return_value=expires):
                expired, = action("report")["events"]
            self.assertNotIn("excerpt", expired)
            self.assertEqual(runtime.manager.store.db.execute("SELECT count(*) FROM excerpts").fetchone()[0], 0)

    def test_native_oversized_write_runs_handler_and_emits_scoped_notice(self):
        from tools import file_tools

        with self.fixture("limits:\n  max_candidate_bytes: 1024\n") as (home, _, runtime):
            target = home / "oversized.txt"
            content = "금지" + "x" * 1024
            streams = [
                EventStream(tool_events("write_file", {"path": str(target), "content": content}, "large-write")),
                EventStream(text_events("완료")),
            ]
            emitted, release, delivered = (threading.Event() for _ in range(3))
            notifications = []

            def post(text):
                notifications.append(text)
                emitted.set()
                release.wait(5)
                delivered.set()
                return SimpleNamespace(result={"ok": True})

            with self.agent(streams) as (agent, requests), \
                 patch.object(runtime.ctx, "post_message", side_effect=post), \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes:
                agent.valid_tool_names = {"write_file"}
                try:
                    result = agent.run_conversation("큰 파일을 작성하세요")
                    self.assertTrue(emitted.wait(2))
                    self.assertFalse(delivered.is_set())
                    self.assertEqual(notifications, ["Guiderails · BYPASSED · Inspection budget exceeded"])
                    self.assertEqual(writes.call_count, 1)
                    self.assertEqual(len(requests), 2)
                finally:
                    release.set()
                self.assertTrue(delivered.wait(2))
            self.assertTrue(result["completed"], result)
            self.assertEqual(len(requests), 2)
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(target.read_bytes(), content.encode("utf-8"))
            self.assertEqual(runtime.manager.store.stats()["detection_count"], 0)

    def test_native_file_namespace_skips_unavailable_backend_and_resolves_local_targets(self):
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        with self.fixture() as (home, _, _):
            target = home / "home" / "target.txt"
            target.write_text("금지", encoding="utf-8")
            for path in ("~/target.txt", str(home / "home" / ".." / "home" / "target.txt")):
                with self.subTest(path=path):
                    message, _ = _dispatch_pre_tool_call_hooks(
                        "patch", {"path": path, "old_string": "금지", "new_string": "정상"})
                    self.assertIsNone(message)
            with patch("hermes_cli.config.load_config_readonly",
                       return_value={"terminal": {"backend": "docker"}}):
                message, _ = _dispatch_pre_tool_call_hooks(
                    "write_file", {"path": str(target), "content": "정상"})
            self.assertIsNone(message)
            message, _ = _dispatch_pre_tool_call_hooks(
                "patch", {"path": target.name, "old_string": "금지", "new_string": "정상"})
            self.assertIsNone(message)
            self.assertEqual(target.read_bytes(), "금지".encode())

    def test_native_group_rejects_duplicate_writes_and_verified_unknown_effect(self):
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.registry import registry

        with self.fixture() as (home, _, _):
            target = home / "group.txt"
            calls = [{"name": "write_file", "arguments": {"path": str(target), "content": "정상"}},
                     {"name": "write_file", "arguments": {"path": str(target), "content": "다른 내용"}}]
            with patch.object(registry, "dispatch", wraps=registry.dispatch) as dispatch:
                message, _ = _dispatch_pre_tool_call_hooks("tool_call", {"calls": calls})
                self.assertIsNotNone(message)
                self.assertFalse(target.exists())
                dispatch.assert_not_called()
        with self.fixture("effect_policy: verified\n") as (_, _, _):
            message, _ = _dispatch_pre_tool_call_hooks("mcp__unknown__change", {"text": "정상"})
            self.assertIsNotNone(message)

    def test_native_structural_errors_take_precedence_over_content_size(self):
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        with self.fixture("limits:\n  max_candidate_bytes: 400\n") as (home, _, runtime):
            target = home / "group.txt"
            alias = str(home / "unused" / ".." / target.name)
            calls = [
                {"name": "write_file", "arguments": {"path": str(target), "content": "첫째"}},
                {"name": "write_file", "arguments": {"path": alias, "content": "둘째"}},
            ]
            patch_body = f"*** Add File: {target}\n+정상\n*** End Patch"
            group_with_patch = [
                calls[0], {"name": "patch", "arguments": {"mode": "patch", "patch":
                    f"*** Begin Patch\n{patch_body}"}},
            ]
            with patch.object(runtime, "_inspection_bypassed") as bypass:
                for padding in ("", "x" * 401):
                    for name, args, error in (
                        ("tool_call", {"calls": calls, "extra": padding}, "parallel_file_conflict"),
                        ("tool_call", {"calls": group_with_patch, "extra": padding}, "parallel_file_conflict"),
                        ("patch", {"mode": "patch", "patch": f"bad{padding}\n{patch_body}"}, "invalid_patch"),
                        ("patch", {"mode": "patch", "patch":
                            f"*** Begin Patch\n*** Move File: {target}\n+{padding}\n*** End Patch"},
                         "unsupported_patch_header"),
                        ("patch", {"mode": "patch", "patch":
                            f"*** Begin Patch\n*** Add File: {target}\ninvalid{padding}\n*** End Patch"},
                         "invalid_add_patch"),
                    ):
                        with self.subTest(tool=name, oversized=bool(padding), error=error):
                            message, _ = _dispatch_pre_tool_call_hooks(name, args, session_id="invalid-call")
                            self.assertEqual((message or "").split("\n")[0], error)
                bypass.assert_not_called()
                args = {"mode": "patch", "patch":
                        f"*** Begin Patch\r\n*** Add File: {target}\r\n+{'x' * 401}\r\n*** End Patch\r\n"}
                message, _ = _dispatch_pre_tool_call_hooks("patch", args, session_id="valid-call")
                self.assertIsNone(message)
                self.assertEqual(bypass.call_args.args[0], "candidate_too_large")
                self.assertEqual(bypass.call_args.args[1]["session_id"], "valid-call")
                self.assertEqual(bypass.call_count, 1)
            self.assertFalse(target.exists())

    def test_candidate_bytes_match_actual_file_handlers(self):
        from guiderails.adapters import ToolInspector
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.registry import registry

        matcher = {"type": "match", "value": "금\r\n지", "case_sensitive": True, "normalization": "none"}
        patterns = document(rule(matcher=matcher, scopes=["artifact.text"],
                                 tests={"positive": ["금\r\n지"], "negative": ["정상"]}))
        with self.fixture(patterns=patterns) as (home, _, runtime):
            inspector = ToolInspector(runtime.manager.engine)
            for mode in ("write", "replace", "v4a"):
                for bom in ("", "\ufeff"):
                    with self.subTest(mode=mode, bom=bool(bom)):
                        target = home / f"{mode}-{int(bool(bom))}.txt"
                        original = (bom + "기존\r\n끝").encode()
                        target.write_bytes(original)
                        name = "write_file" if mode == "write" else "patch"
                        if mode == "write":
                            self.assertFalse(json.loads(registry.dispatch("read_file", {"path": str(target)})).get("error"))
                        for text, blocked in (("금\n지", True), ("교정\n문서", False)):
                            if mode == "write":
                                args = {"path": str(target), "content": text}
                            elif mode == "replace":
                                args = {"path": str(target), "old_string": "기존", "new_string": text}
                            else:
                                additions = "\n".join("+" + line for line in text.split("\n"))
                                args = {"mode": "patch", "patch":
                                        f"*** Begin Patch\n*** Update File: {target}\n@@\n-기존\n{additions}\n*** End Patch"}
                            message, _ = _dispatch_pre_tool_call_hooks(name, args)
                            if blocked:
                                self.assertIn("guiderails.pattern_detected", message)
                                self.assertEqual(target.read_bytes(), original)
                            else:
                                self.assertIsNone(message)
                                predicted = inspector.inspect_call(name, args)
                                self.assertTrue(predicted.allowed, predicted)
                                result = json.loads(registry.dispatch(name, args))
                                self.assertFalse(result.get("error"), result)
                                self.assertEqual(target.read_bytes(), predicted.files[0].after.encode())

    def test_generated_candidates_match_native_bytes_and_preserve_blocked_files(self):
        from guiderails.adapters import ToolInspector
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.registry import registry

        seed = 20261124
        rng = random.Random(seed)
        with self.fixture() as (home, _, runtime):
            inspector = ToolInspector(runtime.manager.engine)
            index = 0
            for mode in ("write", "replace", "v4a"):
                for bom in ("", "\ufeff"):
                    for newline in ("\n", "\r\n"):
                        for size in (4095, 4096, 4097):
                            clean = rng.choice(("", "정상", "e\u0301\t문서"))
                            with self.subTest(seed=seed, index=index, mode=mode, bom=bool(bom),
                                              newline=repr(newline), size=size, clean=repr(clean)):
                                target = home / f"generated-{index}.txt"
                                index += 1
                                remaining = size - len(bom.encode())
                                lines = (remaining - len(newline)) // 64
                                prefix = (("x" * (64 - len(newline)) + newline) * lines
                                          + "x" * (remaining - lines * 64 - len(newline)) + newline)
                                original = (bom + prefix + "기존" + newline + "끝").encode()
                                self.assertEqual(original.index("기존".encode()), size)
                                target.write_bytes(original)
                                name = "write_file" if mode == "write" else "patch"
                                if mode == "write":
                                    result = json.loads(registry.dispatch("read_file", {"path": str(target)}))
                                    self.assertFalse(result.get("error"), result)

                                def arguments(text):
                                    if mode == "write":
                                        return {"path": str(target), "content": text}
                                    if mode == "replace":
                                        return {"path": str(target), "old_string": "기존", "new_string": text}
                                    additions = "\n".join("+" + line for line in text.split("\n"))
                                    return {"mode": "patch", "patch":
                                            f"*** Begin Patch\n*** Update File: {target}\n@@\n"
                                            f"-기존\n{additions}\n*** End Patch"}

                                message, _ = _dispatch_pre_tool_call_hooks(name, arguments("금지"))
                                self.assertIsNotNone(message)
                                self.assertIn("guiderails.pattern_detected", message)
                                self.assertEqual(target.read_bytes(), original)
                                args = arguments(clean)
                                predicted = inspector.inspect_call(name, args)
                                self.assertTrue(predicted.allowed, predicted)
                                message, _ = _dispatch_pre_tool_call_hooks(name, args)
                                self.assertIsNone(message)
                                result = json.loads(registry.dispatch(name, args))
                                self.assertFalse(result.get("error"), result)
                                self.assertEqual(target.read_bytes(), predicted.files[0].after.encode())

    def test_control_characters_and_v4a_terminal_candidates(self):
        from guiderails.adapters import ToolInspector
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.registry import registry

        matcher = {"type": "match", "value": "금\t지", "case_sensitive": True, "normalization": "none"}
        patterns = document(rule(matcher=matcher, scopes=["artifact.text"],
                                 tests={"positive": ["금\t지"], "negative": ["정상"]}))
        with self.fixture(patterns=patterns) as (home, _, runtime):
            inspector = ToolInspector(runtime.manager.engine)
            for mode in ("replace", "v4a"):
                with self.subTest(mode=mode):
                    target = home / f"control-{mode}.txt"
                    original = "기존\t문장"
                    target.write_bytes(original.encode())
                    if mode == "replace":
                        bad = {"path": str(target), "old_string": original, "new_string": r"금\t지"}
                        clean = {"path": str(target), "old_string": original, "new_string": r"교정\t문서"}
                    else:
                        def args(text):
                            return {"mode": "patch", "patch": (
                                f"*** Begin Patch\n*** Update File: {target}\n@@\n"
                                f"-{original}\n+{text}\n*** End Patch")}
                        bad, clean = args(r"금\t지"), args(r"교정\t문서")
                    message, _ = _dispatch_pre_tool_call_hooks("patch", bad)
                    self.assertIn("guiderails.pattern_detected", message)
                    self.assertEqual(target.read_bytes(), original.encode())
                    predicted = inspector.inspect_call("patch", clean)
                    self.assertTrue(predicted.allowed, predicted)
                    message, _ = _dispatch_pre_tool_call_hooks("patch", clean)
                    self.assertIsNone(message)
                    result = json.loads(registry.dispatch("patch", clean))
                    self.assertFalse(result.get("error"), result)
                    self.assertEqual(target.read_bytes(), predicted.files[0].after.encode())

        matcher = {"type": "regex", "pattern": r"금지\Z", "flags": [], "normalization": "none"}
        with self.fixture(patterns=document(rule(matcher=matcher, scopes=["artifact.text"]))) as (home, _, _):
            target = home / "added.txt"
            def add(text):
                return {"mode": "patch", "patch":
                        f"*** Begin Patch\n*** Add File: {target}\n+{text}\n*** End Patch"}
            message, _ = _dispatch_pre_tool_call_hooks("patch", add("금지"))
            self.assertIn("guiderails.pattern_detected", message)
            self.assertFalse(target.exists())
            message, _ = _dispatch_pre_tool_call_hooks("patch", add("정상"))
            self.assertIsNone(message)
            result = json.loads(registry.dispatch("patch", add("정상")))
            self.assertFalse(result.get("error"), result)
            self.assertEqual(target.read_bytes(), "정상".encode())

    def test_ambiguous_write_recovers_with_exact_edit_in_same_turn(self):
        from tools import file_tools

        with self.fixture() as (home, _, _):
            target = home / "long-line.txt"
            original = "가" * 1400 + "\r\n끝"
            target.write_bytes(original.encode())
            streams = [
                EventStream(tool_events("write_file", {"path": str(target), "content": "교정\n문서"}, "write")),
                EventStream(tool_events("patch", {"path": str(target), "old_string": original,
                                                 "new_string": "교정\n문서"}, "exact")),
                EventStream(text_events("정리했습니다.")),
            ]
            with self.agent(streams) as (agent, requests), \
                 patch.object(file_tools, "write_file_tool", wraps=file_tools.write_file_tool) as writes, \
                 patch.object(file_tools, "patch_tool", wraps=file_tools.patch_tool) as edits:
                agent.valid_tool_names = {"write_file", "patch"}
                result = agent.run_conversation("문서를 정리하세요")
            self.assertTrue(result["completed"], result)
            self.assertEqual((writes.call_count, edits.call_count), (0, 1))
            self.assertEqual(target.read_bytes(), "교정\r\n문서".encode())
            self.assertEqual(len(requests), 3)
            self.assertIn("ambiguous_file_format", str(requests[1]["input"]))
            self.assertEqual(sum(row.get("role") == "user" for row in result["messages"]), 1)
            self.assertTrue(all(stream.closed for stream in streams))
