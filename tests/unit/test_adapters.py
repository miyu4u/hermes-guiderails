import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest import mock

from guiderails.adapters import ToolInspector
from guiderails.engine import Limits, RuleEngine


def engine(scopes=("tool.arguments", "artifact.text"), limits=None, needle="BAD", **rule_fields):
    rule = {
        "id": "fixture.forbidden",
        "revision": 1,
        "enabled": True,
        "description": "forbidden marker",
        "priority": 100,
        "scopes": list(scopes),
        "matcher": {"type": "match", "value": needle, "case_sensitive": True, "normalization": "none"},
        "edit_policy": "whole_result",
        "announce": {"instruction": "Use a safe expression."},
        "tests": {"positive": [needle], "negative": ["good"]},
        **rule_fields,
    }
    return RuleEngine.from_yaml(json.dumps({"schema_version": 1, "rules": [rule]}), limits=limits)


class CandidateTests(unittest.TestCase):
    def test_multi_selector_matches_array_paths_once_without_joining_values(self):
        tool = "docs.update"
        selectors = [
            {"tool": tool, "path": "/changes/*/a~1b~0c"},
            {"tool": tool, "path": "/changes/1/a~1b~0c"},
            {"tool": tool, "path": "/text"},
            {"tool": "docs.create", "path": "/text"},
        ]
        inspector = ToolInspector(engine(("tool.arguments",), arguments=selectors, limits=Limits(max_hits=1)))
        args = {"changes": [{"a/b~c": "BA"}, {"a/b~c": "line\nBAD"}, {"a/b~c": "D"}],
                "text": 42, "old_str": "BAD"}
        result = inspector.inspect_call(tool, args)
        self.assertEqual(result.error_code, "")
        self.assertFalse(result.allowed)
        self.assertEqual([(h.field_path, h.start, h.end, h.line, h.column) for h in result.hits],
                         [("/changes/1/a~1b~0c", 5, 8, 2, 1)])
        grouped = inspector.inspect_call("parallel", {"tool_uses": [{"recipient_name": tool, "parameters": args}]})
        self.assertEqual([h.field_path for h in grouped.hits],
                         ["/tool_uses/0/parameters/changes/1/a~1b~0c"])
        self.assertTrue(inspector.inspect_call("update", args).allowed)
        self.assertTrue(inspector.inspect_call("docs.create", args).allowed)
        self.assertFalse(inspector.inspect_call("docs.create", {"text": "BAD"}).allowed)
        args["changes"][1]["a/b~c"] = None
        self.assertTrue(inspector.inspect_call(tool, args).allowed)
        for changes in ([], [{"missing": "BAD"}], {"*": {"a/b~c": "BAD"}}):
            self.assertTrue(inspector.inspect_call(tool, dict(args, changes=changes)).allowed)

    def test_only_exact_selector_accepts_literal_object_star(self):
        args = {"changes": {"*": "BAD", "a*": "BAD", "other": "good"}}
        wildcard = ToolInspector(engine(("tool.arguments",), arguments=[{"tool": "docs", "path": "/changes/*"}]))
        exact = ToolInspector(engine(("tool.arguments",), argument={"tool": "docs", "path": "/changes/*"}))
        partial = ToolInspector(engine(("tool.arguments",), arguments=[{"tool": "docs", "path": "/changes/a*"}]))
        self.assertTrue(wildcard.inspect_call("docs", args).allowed)
        self.assertEqual([h.field_path for h in exact.inspect_call("docs", args).hits], ["/changes/*"])
        self.assertEqual([h.field_path for h in partial.inspect_call("docs", args).hits], ["/changes/a*"])

    def test_multi_selector_does_not_override_verified_effect_or_file_errors(self):
        rules = engine(("tool.arguments",), arguments=[
            {"tool": "docs.update", "path": "/updates/*/content"},
            {"tool": "patch", "path": "/new_string"},
        ])
        inspector = ToolInspector(rules, effect_policy="verified")
        result = inspector.inspect_call("docs.update", {"updates": [{"content": "BAD"}]}, first=True)
        self.assertEqual(result.error_code, "unsupported_tool")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "existing"
            target.write_text("preserve", encoding="utf-8")
            result = inspector.inspect_call(
                "patch", {"path": str(target), "old_string": "missing", "new_string": "BAD"}, first=True)
            self.assertEqual(result.error_code, "ambiguous_or_fuzzy_edit")
            self.assertEqual(result.hits, ())

    def test_explicit_argument_rule_matches_raw_field_not_file_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_text("BAD elsewhere\nBAD replace\n", encoding="utf-8")
            args = {"path": str(target), "old_string": "BAD replace", "new_string": "good"}
            for field in ("old_string", "new_string"):
                with self.subTest(field=field):
                    inspector = ToolInspector(engine(("tool.arguments",), argument={"tool": "patch", "path": "/" + field}))
                    result = inspector.inspect_call("patch", args, first=True)
                    self.assertEqual(result.allowed, field == "new_string")
                    self.assertEqual(result.files[0].after, "BAD elsewhere\ngood\n")
                    incoming = inspector.inspect_call("patch", dict(args, new_string="BAD new"), first=True)
                    self.assertEqual(incoming.hits[0].field_path, "/" + field)
                    self.assertEqual(incoming.hits[0].start, 0)
                    self.assertTrue(inspector.inspect_call("other.patch", args).allowed)

    def test_argument_selector_in_nested_call_uses_child_name_and_escaped_pointer(self):
        inspector = ToolInspector(engine(("tool.arguments",), argument={
            "tool": "docs.update", "path": "/changes/0/a~1b~0c",
        }))
        args = {"changes": [{"a/b~c": "good"}, {"a/b~c": "BAD"}], "oldText": "BAD"}
        self.assertTrue(inspector.inspect_call("docs.update", args).allowed)
        args["changes"][0]["a/b~c"] = "BAD"
        self.assertTrue(inspector.inspect_call("update", args).allowed)
        result = inspector.inspect_call("tool_call", {"calls": [{"name": "docs.update", "arguments": args}]})
        self.assertEqual([hit.field_path for hit in result.hits], ["/calls/0/arguments/changes/0/a~1b~0c"])

    def test_namespace_absolute_tilde_dotdot_and_denials(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            target = home / "doc"
            target.write_text("BA middle D", encoding="utf-8")
            context = {"local": True, "base": None, "home": str(home)}
            inspector = ToolInspector(engine(("artifact.text",)), path_context=context)
            for spelling in (str(target), "~/doc", str(home / "missing" / ".." / "doc")):
                with self.subTest(path=spelling):
                    result = inspector.inspect_call("patch", {
                        "path": spelling, "old_string": " middle ", "new_string": "",
                    })
                    self.assertFalse(result.allowed)
                    self.assertEqual(result.files[0].path, str(target))
                    self.assertEqual(result.files[0].after, "BAD")
            for context, path, code in (
                (context, "doc", "path_context_unavailable"),
                (context, "~other/doc", "path_context_unavailable"),
                ({"local": False}, str(target), "unsupported_file_backend"),
                ({"local": True, "home": None}, "~/doc", "file_context_unavailable"),
                (lambda: None, str(target), "file_context_unavailable"),
            ):
                with self.subTest(code=code):
                    reader = mock.Mock(side_effect=AssertionError("unexpected local read"))
                    result = ToolInspector(engine(), reader=reader, path_context=context).inspect_call(
                        "write_file", {"path": path, "content": "good"})
                    self.assertEqual(result.error_code, code)
                    reader.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "BA middle D")

    def test_candidate_preserves_bom_crlf_and_control_replacements(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_bytes(b"\xef\xbb\xbfhead\r\nold\tvalue\r\n")
            inspector = ToolInspector(engine())
            result = inspector.inspect_call("patch", {
                "path": str(target), "old_string": "old\tvalue", "new_string": r"new\tvalue",
            })
            self.assertTrue(result.allowed)
            self.assertEqual(result.files[0].after.encode(), b"\xef\xbb\xbfhead\r\nnew\tvalue\r\n")
            write = inspector.inspect_call("write_file", {"path": str(target), "content": "good\nnext\n"})
            self.assertEqual(write.files[0].after.encode(), b"\xef\xbb\xbfgood\r\nnext\r\n")
            self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfhead\r\nold\tvalue\r\n")

    def test_write_probe_disagreement_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_bytes(("é" * 2048 + "\r\n").encode())
            result = ToolInspector(engine()).inspect_call("write_file", {
                "path": str(target), "content": "good\nnext",
            })
            self.assertEqual(result.error_code, "ambiguous_file_format")

    def test_replace_all_bounds_formatted_utf8_candidate_before_materializing(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            inspector = ToolInspector(engine(limits=Limits(max_candidate_bytes=1024)))
            args = {"path": str(target), "old_string": "x", "new_string": "é\n" * 25, "replace_all": True}
            for suffix in ("z", "zz"):
                target.write_bytes(("\ufeff" + "x\r\n" * 10 + suffix).encode())
                result = inspector.inspect_call("patch", args)
                if suffix == "z":
                    expected = "\ufeff" + ("é\r\n" * 25 + "\r\n") * 10 + suffix
                    self.assertEqual(len(expected.encode()), 1024)
                    self.assertTrue(result.allowed)
                    self.assertEqual(result.files[0].after, expected)
                else:
                    self.assertEqual(result.error_code, "candidate_too_large")

            target.write_bytes(b"x" * 900)
            args["new_string"] = "y" * 900  # Unbounded replacement allocates 810 KB.
            tracemalloc.start()
            try:
                baseline = tracemalloc.get_traced_memory()[0]
                tracemalloc.reset_peak()
                result = inspector.inspect_call("patch", args)
                peak = tracemalloc.get_traced_memory()[1] - baseline
            finally:
                tracemalloc.stop()
            self.assertEqual(result.error_code, "candidate_too_large")
            self.assertLess(peak, 400_000)

    def test_new_argument_inspected_when_host_converts_control_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_text("old\tvalue", encoding="utf-8")
            inspector = ToolInspector(engine(("tool.arguments",), needle=r"\t"))
            result = inspector.inspect_call("patch", {
                "path": str(target), "old_string": "old\tvalue", "new_string": r"new\tvalue",
            })
            self.assertFalse(result.allowed)
            self.assertEqual(result.files[0].after, "new\tvalue")
            self.assertEqual(result.hits[0].field_path, "/new_string")

    def test_removal_retained_and_fuzzy_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_text("BAD first\nBAD second", encoding="utf-8")
            inspector = ToolInspector(engine(("artifact.text",)))
            retained = inspector.inspect_call("patch", {
                "path": str(target), "old_string": "BAD first", "new_string": "good",
            })
            self.assertEqual(len(retained.hits), 1)
            removed = inspector.inspect_call("patch", {
                "path": str(target), "old_string": "BAD", "new_string": "good", "replace_all": True,
            })
            self.assertTrue(removed.allowed)
            self.assertEqual(removed.files[0].after, "good first\ngood second")
            self.assertEqual(inspector.inspect_call("patch", {
                "path": str(target), "old_string": "BAD", "new_string": "good",
            }).error_code, "ambiguous_or_fuzzy_edit")
            self.assertEqual(target.read_text(), "BAD first\nBAD second")

    def test_v4a_add_update_delete_and_unsupported_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            target, new = home / "doc", home / "new"
            target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
            inspector = ToolInspector(engine())
            update = f"*** Begin Patch\n*** Update File: {target}\n@@\n-first\n+good\n*** End Patch"
            result = inspector.inspect_call("patch", {"mode": "patch", "patch": update})
            self.assertTrue(result.allowed)
            self.assertEqual(result.files[0].after.encode(), b"\xef\xbb\xbfgood\r\nsecond\r\n")
            add = f"*** Begin Patch\n*** Add File: {new}\n+good\n+\n*** End Patch"
            result = inspector.inspect_call("patch", {"mode": "patch", "patch": add})
            self.assertEqual(result.files[0].after, "good\n")
            delete = f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch"
            self.assertIsNone(inspector.inspect_call("patch", {"mode": "patch", "patch": delete}).files[0].after)
            move = f"*** Begin Patch\n*** Move File: {target} -> {new}\n*** End Patch"
            self.assertEqual(inspector.inspect_call("patch", {"mode": "patch", "patch": move}).error_code,
                             "unsupported_patch_header")
            self.assertFalse(new.exists())

    def test_nonregular_oversized_and_invalid_utf8_are_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            target = home / "doc"
            arguments = {"path": str(target), "content": "good"}
            # Leave room for the request itself even under a long sandbox path.
            limit = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
            inspector = ToolInspector(engine(limits=Limits(max_candidate_bytes=limit)))
            target.write_bytes(b"\xff")
            self.assertEqual(inspector.inspect_call("write_file", arguments).error_code, "tool_read_failed")
            target.write_bytes(b"x" * (limit + 1))
            self.assertEqual(inspector.inspect_call("write_file", arguments).error_code, "candidate_too_large")
            target.unlink()
            os.mkfifo(target)
            self.assertEqual(inspector.inspect_call("write_file", arguments).error_code, "nonregular_file")
            target.unlink()
            target.symlink_to(home / "missing")
            self.assertEqual(inspector.inspect_call("write_file", arguments).error_code, "nonregular_file")

    def test_final_symlink_swap_during_open_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            target, secret = home / "doc", home / "secret"
            target.write_text("good", encoding="utf-8")
            secret.write_text("BAD", encoding="utf-8")
            original_open = os.open
            inspector = ToolInspector(engine())

            def swap(path, flags):
                target.unlink()
                target.symlink_to(secret)
                return original_open(path, flags)

            with mock.patch("guiderails.fileio.os.open", side_effect=swap):
                result = inspector.inspect_call("write_file", {"path": str(target), "content": "good"})
            self.assertEqual(result.error_code, "nonregular_file")
            self.assertEqual(secret.read_text(), "BAD")

    def test_revalidation_rejects_changed_source_and_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve() / "doc"
            target.write_text("old", encoding="utf-8")
            inspector = ToolInspector(engine())
            args = {"path": str(target), "old_string": "old", "new_string": "good"}
            result = inspector.inspect_call("patch", args)
            self.assertTrue(inspector.revalidate(result, "patch", args))
            self.assertFalse(inspector.revalidate(result, "patch", dict(args, new_string="other")))
            target.write_text("changed", encoding="utf-8")
            self.assertFalse(inspector.revalidate(result, "patch", args))


class EffectTests(unittest.TestCase):
    def test_document_scope_and_trusted_revision(self):
        for policy in ("pattern", "verified"):
            inspector = ToolInspector(engine(("artifact.text",)), effect_policy=policy)
            inspector.register_tool("document", effect="document", revision_verified=True)
            inspector.register_tool("unverified", effect="document")
            self.assertTrue(inspector.inspect_call("unregistered", {"text": "BAD"}).allowed == (policy == "pattern"))
            result = inspector.inspect_call("document", {"text": "BAD"})
            self.assertFalse(result.allowed)
            self.assertEqual(len(result.hits), 1)
            result = inspector.inspect_call("unverified", {"text": "good"})
            self.assertEqual(result.error_code, "" if policy == "pattern" else "unverified_external_revision")
            self.assertEqual(inspector.inspect_call("execute_code", {"code": "good"}).error_code,
                             "" if policy == "pattern" else "opaque_effect")

    def test_exact_builtin_reads_and_nested_group_failures(self):
        inspector = ToolInspector(engine(), effect_policy="verified")
        inspector.register_tool("external_reader", effect="read", revision_verified=True)
        self.assertTrue(inspector.inspect_call("read_file", {"path": "BAD"}).allowed)
        self.assertFalse(inspector.inspect_call("external_reader", {"path": "BAD"}).allowed)
        self.assertEqual(inspector.inspect_call("mcp.read_file", {"path": "BAD"}).error_code, "unsupported_tool")
        result = inspector.inspect_call("tool_call", {"calls": [
            {"name": "read_file", "arguments": {"path": "BAD"}},
            {"name": "unknown", "arguments": {"text": "BAD"}},
        ]})
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, "unsupported_tool")
        self.assertEqual(result.hits[0].field_path, "/calls/1/arguments/text")
        self.assertEqual(inspector.inspect_call("parallel", {"tool_uses": [None]}).error_code,
                         "invalid_nested_calls")

    def test_nested_duplicate_file_and_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            target = str(Path(folder).resolve() / "doc")
            inspector = ToolInspector(engine())
            result = inspector.inspect_call("parallel", {"tool_uses": [
                {"recipient_name": "functions.write_file", "parameters": {"path": target, "content": "good"}},
                {"recipient_name": "functions.write_file", "parameters": {"path": target, "content": "good"}},
            ]})
            self.assertEqual(result.error_code, "parallel_file_conflict")
            limited = ToolInspector(engine(limits=Limits(max_candidate_bytes=64)))
            self.assertEqual(limited.inspect_call("read_file", {"path": "x" * 128}).error_code,
                             "candidate_too_large")


if __name__ == "__main__":
    unittest.main()
