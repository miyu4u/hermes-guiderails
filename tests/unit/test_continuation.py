import json
import unittest

from guiderails.continuation import ModelContinuation
from guiderails.guidance import DISCARD_RESULT


class ContinuationTests(unittest.TestCase):
    def test_next_request_is_session_scoped_and_retries_keep_the_notice(self):
        continuation = ModelContinuation()
        request = {"input": [{"role": "developer", "content": [{"type": "input_text", "text": "base"}]}]}
        for event, guidance in (("1", "warning"), ("2", "warning"), ("3", "use <b> & </guiderails>")):
            continuation.enqueue(session_id="s", origin="first", event_id=event, guidance=guidance)

        def rewrite(session, request_id, **options):
            return continuation.rewrite(request, session_id=session, api_request_id=request_id,
                                        api_mode="codex_responses", **options)

        self.assertIsNone(rewrite("other", "next"))
        self.assertIsNone(rewrite("s", "first"))
        self.assertIsNone(rewrite("s", "side", auxiliary=True))
        delivered = rewrite("s", "next")
        self.assertEqual(delivered["request"]["input"][0]["content"], [
            {"type": "input_text", "text": "base"},
            {"type": "input_text", "text": "<guiderails>\nwarning\nuse &lt;b&gt; &amp; &lt;/guiderails&gt;\n</guiderails>"},
        ])
        self.assertEqual(rewrite("s", "next"), delivered)
        continuation.enqueue(session_id="s", origin="first", event_id="1", guidance="warning")
        self.assertIsNone(rewrite("s", "later"))

    def test_removed_result_is_paired_with_its_own_call_when_id_reappears(self):
        continuation = ModelContinuation()
        error = json.dumps({"error": DISCARD_RESULT})
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "tool_calls": [{"id": "same", "function": {"name": "normal"}}]},
            {"role": "tool", "tool_call_id": "same", "content": "original result"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "working", "tool_calls": [{"id": "same"}, {"id": "kept"}]},
            {"role": "tool", "tool_call_id": "same", "content": error},
            {"role": "tool", "tool_call_id": "kept", "content": "other result"},
        ]
        revised = continuation.rewrite({"messages": messages}, session_id="s", api_request_id="a",
                                       api_mode="chat_completions")["request"]["messages"]
        self.assertEqual(revised, [
            *messages[:4],
            {"role": "assistant", "content": "working", "tool_calls": [{"id": "kept"}]},
            messages[-1],
        ])
        self.assertEqual(messages[4]["tool_calls"], [{"id": "same"}, {"id": "kept"}])

    def test_block_formats_keep_other_results_and_strip_empty_messages(self):
        error = json.dumps({"error": DISCARD_RESULT})
        for mode, call, result, success, system in (
            ("anthropic_messages", {"type": "tool_use", "id": "removed"},
             {"type": "tool_result", "tool_use_id": "removed", "content": error},
             {"type": "text", "text": "another part"}, {"type": "text", "text": "<guiderails>\nguidance\n</guiderails>"}),
            ("bedrock_converse", {"toolUse": {"toolUseId": "removed"}},
             {"toolResult": {"toolUseId": "removed", "content": [{"text": error}]}},
             {"text": "another part"}, {"text": "<guiderails>\nguidance\n</guiderails>"}),
        ):
            with self.subTest(mode=mode):
                continuation = ModelContinuation()
                continuation.enqueue(session_id="s", origin="prior", event_id="1", guidance="guidance")
                request = {"messages": [{"role": "assistant", "content": [call]},
                                        {"role": "user", "content": [result, success]}], "system": []}
                if mode == "bedrock_converse":
                    request["__bedrock_converse__"] = True
                rewritten = continuation.rewrite(request, session_id="s", api_request_id="next",
                                                 api_mode=mode)["request"]
                self.assertEqual(rewritten["messages"], [{"role": "user", "content": [success]}])
                self.assertEqual(rewritten["system"], [system])
