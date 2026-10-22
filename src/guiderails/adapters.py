"""Side-effect-free tool inspection; dispatch and file locking belong to Hermes."""

from concurrent.futures import Future, TimeoutError
from contextvars import copy_context
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from threading import BoundedSemaphore, Thread
import time

from .engine import InspectionError, RESOURCE_LIMITS
from .fileio import FileIOError, FileTooLargeError, NonRegularFileError, read_bytes

_FILE_SLOTS = BoundedSemaphore(4)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise InspectionError("inspection_deadline")


def _validate_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise InspectionError("invalid_or_unreadable_tool_payload")


def _patch_files(patch, deadline, *, timeout_code):
    """Check the supported V4A syntax, yielding body ranges without copying bodies."""
    begin, end = "*** Begin Patch", "*** End Patch"
    start = len(begin)
    if patch.startswith("\r\n", start):
        start += 2
    elif patch.startswith("\n", start):
        start += 1
    else:
        raise InspectionError("invalid_patch")
    stop = len(patch)
    if patch.endswith("\n"):
        stop -= 1
    if patch.endswith("\r", 0, stop):
        stop -= 1
    stop -= len(end)
    if (not patch.startswith(begin) or stop < start
            or not patch.startswith(end, stop) or patch[stop - 1] != "\n"):
        raise InspectionError("invalid_patch")
    if start == stop:
        raise InspectionError("empty_patch")

    def line_end(index):
        if time.monotonic() >= deadline:
            raise InspectionError(timeout_code)
        return patch.index("\n", index, stop)

    while start < stop:
        header_end = line_end(start)
        for kind in ("Add", "Update", "Delete"):
            prefix = f"*** {kind} File: "
            if patch.startswith(prefix, start, header_end):
                break
        else:
            raise InspectionError("unsupported_patch_header")
        path_start = start + len(prefix)
        # Paths are metadata, not an unbounded copy of a malformed header.
        if header_end - path_start > 4096:
            raise InspectionError("invalid_or_unreadable_tool_payload")
        path = patch[path_start:header_end].strip()
        _validate_path(path)
        body = start = header_end + 1
        if kind == "Add":
            while start < stop and patch.startswith("+", start):
                start = line_end(start) + 1
            if start == body or (start < stop and not patch.startswith("*** ", start)):
                raise InspectionError("invalid_add_patch")
        elif kind == "Update":
            changed = old = any_group = False
            while start < stop and not patch.startswith("*** ", start):
                if patch.startswith("@@", start):
                    if changed and not old:
                        raise InspectionError("ambiguous_insertion_patch")
                    any_group |= changed
                    changed = old = False
                elif patch[start] in " -+":
                    changed = True
                    old |= patch[start] != "+"
                else:
                    raise InspectionError("unsupported_patch_line")
                start = line_end(start) + 1
            if changed and not old:
                raise InspectionError("ambiguous_insertion_patch")
            if not (any_group or changed):
                raise InspectionError("empty_patch")
        yield kind, path, body, start


def validate_read_arguments(name, arguments):
    required = "path" if name == "read_file" else "pattern"
    if not isinstance(arguments.get(required), str):
        raise InspectionError("invalid_tool_payload")
    if name == "read_file":
        _validate_path(arguments["path"])
        fields = {"offset": int, "limit": int}
    else:
        fields = dict.fromkeys(("path", "target", "file_glob", "order", "output_mode"), str)
        fields.update(dict.fromkeys(("limit", "offset", "context"), int))
    if any(key in arguments and type(arguments[key]) is not expected for key, expected in fields.items()):
        raise InspectionError("invalid_tool_payload")


def check_argument_budget(arguments, limit, deadline):
    # Avoid encoding common giant write/command arguments into a second copy.
    if any(isinstance(value, str) and len(value) > limit for value in arguments.values()):
        raise InspectionError("candidate_too_large")
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(arguments):
        if len(chunk) > limit - size:
            raise InspectionError("candidate_too_large")
        size += len(chunk.encode("utf-8"))
        if size > limit:
            raise InspectionError("candidate_too_large")
        if time.monotonic() >= deadline:
            raise InspectionError("inspection_deadline")


def payload_digest(name, arguments):
    raw = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def content_digest(text):
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def _strings(value, deadline, path=()):
    _check_deadline(deadline)
    if len(path) > 32:
        raise InspectionError("tool_nesting_limit")
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, deadline, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, deadline, (*path, index))


def _pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _replacement_parts(content, old, new):
    start = 0
    while (index := content.find(old, start)) != -1:
        yield content, start, index
        yield new, 0, len(new)
        start = index + len(old)
    yield content, start, len(content)


def _bounded_replacement(before, parts, limit, deadline):
    """Encode the formatted result incrementally, retaining at most limit bytes."""
    offset = int(before.startswith("\ufeff"))
    sample = before[offset:offset + 4096]
    ending = "\r\n" if "\r\n" in sample else "\n" if "\n" in sample else ""
    output = bytearray()
    pending_cr = False

    def append(text):
        encoded = text.encode("utf-8")
        if len(encoded) > limit - len(output):
            raise InspectionError("candidate_too_large")
        output.extend(encoded)

    for text, start, end in parts:
        _check_deadline(deadline)
        for pos in range(start, end, 4096):
            _check_deadline(deadline)
            chunk = text[pos:min(pos + 4096, end)]
            if ending:
                if pending_cr:
                    chunk = "\r" + chunk
                pending_cr = chunk.endswith("\r")
                if pending_cr:
                    chunk = chunk[:-1]
                chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
                if ending == "\r\n":
                    chunk = chunk.replace("\n", "\r\n")
            if not output and offset and chunk and not chunk.startswith("\ufeff"):
                append("\ufeff")
            append(chunk)
    if not output and offset:
        append("\ufeff")
    if pending_cr:
        append(ending)
    return output.decode("utf-8")


@dataclass(frozen=True)
class FileCandidate:
    path: str
    before_digest: str | None
    after: str | None


@dataclass(frozen=True)
class ToolInspection:
    allowed: bool
    hits: tuple = ()
    digest: str = ""
    ruleset_digest: str = ""
    files: tuple = ()
    error_code: str = ""
    coverage: str = "arguments"


class ToolInspector:
    def __init__(self, engine, *, reader=None, root=None, effect_policy="pattern", path_context=None):
        if effect_policy not in ("pattern", "verified"):
            raise ValueError("invalid_effect_policy")
        self.engine = engine
        self.reader = reader or self._read
        self.root = Path(root).resolve() if root else None
        self.effect_policy = effect_policy
        self.path_context = path_context
        self.policies = {}

    def register_tool(self, name, *, effect, revision_verified=False):
        if effect not in ("read", "document"):
            raise ValueError("unknown_tool_effect")
        self.policies[name] = (effect, revision_verified)

    def _namespace_path(self, value):
        _validate_path(value)
        context = self.path_context() if callable(self.path_context) else self.path_context
        if callable(self.path_context) and context is None:
            raise InspectionError("file_context_unavailable")
        if context is not None:
            if not isinstance(context, dict) or not isinstance(context.get("local"), bool):
                raise InspectionError("file_context_unavailable")
            if not context["local"]:
                raise InspectionError("unsupported_file_backend")
            if value == "~" or value.startswith("~/"):
                home = context.get("home")
                if not isinstance(home, str) or not Path(home).is_absolute():
                    raise InspectionError("file_context_unavailable")
                value = home + value[1:]
            elif value.startswith("~"):
                raise InspectionError("path_context_unavailable")
        try:
            path = Path(value).expanduser()
        except RuntimeError as exc:
            raise InspectionError("invalid_or_unreadable_tool_payload") from exc
        if not path.is_absolute():
            if context is not None:
                base = context.get("base")
                if not isinstance(base, str) or not Path(base).is_absolute():
                    raise InspectionError("path_context_unavailable")
                path = Path(base) / path
            else:
                path = path.absolute()
        return path

    def _path(self, value):
        path = self._namespace_path(value)
        # Never resolve the final component: read_bytes must reject a symlink
        # even if the component is replaced between inspection and open.
        if path.is_symlink():
            raise InspectionError("nonregular_file")
        if any(part.is_symlink() for part in path.parents):
            raise InspectionError("symlink_file")
        path = Path(os.path.abspath(path))
        if self.root and not path.is_relative_to(self.root):
            raise InspectionError("untrusted_path")
        return str(path)

    def _read(self, path):
        try:
            return read_bytes(path, self.engine.limits.max_candidate_bytes).decode("utf-8")
        except FileNotFoundError:
            return None
        except NonRegularFileError as exc:
            raise InspectionError("nonregular_file") from exc
        except FileTooLargeError as exc:
            raise InspectionError("candidate_too_large") from exc
        except FileIOError as exc:
            raise InspectionError("tool_read_failed") from exc

    @staticmethod
    def _file_work(work, deadline):
        _check_deadline(deadline)
        if not _FILE_SLOTS.acquire(blocking=False):
            raise InspectionError("inspection_limit")
        result = Future()

        def run():
            try:
                result.set_result(work())
            except BaseException as exc:
                result.set_exception(exc)
            finally:
                _FILE_SLOTS.release()

        # A stalled filesystem operation cannot be cancelled by Python. Keep
        # its slot occupied until it exits, without holding up shutdown.
        try:
            Thread(target=copy_context().run, args=(run,), daemon=True,
                   name="guiderails-file-read").start()
        except RuntimeError as exc:
            _FILE_SLOTS.release()
            raise InspectionError("inspection_limit") from exc
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            if result.done() and result.exception() is exc:
                raise InspectionError("tool_read_failed") from exc
            raise InspectionError("inspection_deadline") from exc

    def _read_candidate(self, value, deadline):
        def read():
            path = self._path(value)
            return path, self.reader(path)
        return self._file_work(read, deadline)

    @staticmethod
    def _normalize_endings(text, original):
        sample = original[:4096]
        ending = "\r\n" if "\r\n" in sample else "\n" if "\n" in sample else ""
        if ending == "\r\n":
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            text = text.replace("\n", "\r\n")
        return text

    @classmethod
    def _preserve_format(cls, before, after, *, operation):
        original = before or ""
        if operation == "write":
            normalized = cls._normalize_endings(after, original)
            sample = original.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
            if normalized != cls._normalize_endings(after, sample):
                raise InspectionError("ambiguous_file_format")
            after = normalized
        else:
            # V4A passes BOM-stripped read_file_raw content as pre_content.
            after = cls._normalize_endings(after, original.removeprefix("\ufeff"))
        if original.startswith("\ufeff") and not after.startswith("\ufeff"):
            after = "\ufeff" + after
        return after

    @staticmethod
    def _replacement_text(old, new):
        if old != new:
            for literal, control in (("\\t", "\t"), ("\\r", "\r")):
                if control in old:
                    new = new.replace(literal, control)
        return new

    def _replace(self, arguments, deadline):
        old, new = arguments["old_string"], arguments["new_string"]
        replace_all = arguments.get("replace_all", False)
        path, before = self._read_candidate(arguments["path"], deadline)
        if before is None:
            raise InspectionError("missing_file")
        content = before.removeprefix("\ufeff")
        count = content.count(old)
        if count == 0 or (count > 1 and not replace_all):
            raise InspectionError("ambiguous_or_fuzzy_edit")
        incoming = new
        new = self._replacement_text(old, new)
        after = _bounded_replacement(before, _replacement_parts(content, old, new),
                                     self.engine.limits.max_candidate_bytes, deadline)
        return [(path, before, after, "/new_string", (incoming,))]

    def _v4a(self, patch, deadline):
        output, seen = [], set()
        for kind, value, start, end in _patch_files(patch, deadline, timeout_code="inspection_deadline"):
            path, before = self._read_candidate(value, deadline)
            if path in seen:
                raise InspectionError("duplicate_patch_path")
            seen.add(path)
            if kind == "Delete":
                if before is None:
                    raise InspectionError("missing_file")
                output.append((path, before, None, "/patch", ()))
                continue
            body = [line.removesuffix("\r") for line in patch[start:end - 1].split("\n")]
            if kind == "Add":
                if before is not None:
                    raise InspectionError("invalid_add_patch")
                after = "\n".join(line[1:] for line in body)
                introduced = (after,)
            else:
                if before is None:
                    raise InspectionError("missing_file")
                after = before.removeprefix("\ufeff")
                groups, old, new, added = [], [], [], []
                introduced = []
                for line in body + ["@@"]:
                    if added and not line.startswith("+"):
                        introduced.append("\n".join(added))
                        added = []
                    if line.startswith("@@"):
                        if old or new:
                            groups.append((old, new))
                            old, new = [], []
                    elif line.startswith(" "):
                        old.append(line[1:])
                        new.append(line[1:])
                    elif line.startswith("-"):
                        old.append(line[1:])
                    elif line.startswith("+"):
                        new.append(line[1:])
                        added.append(line[1:])
                for old, new in groups:
                    old_text = "\n".join(old)
                    if not old_text or after.count(old_text) != 1:
                        raise InspectionError("ambiguous_or_fuzzy_patch")
                    if old != new:
                        after = after.replace(old_text, self._replacement_text(old_text, "\n".join(new)), 1)
                after = self._preserve_format(before, after, operation="v4a")
            output.append((path, before, after, "/patch", introduced))
        return output

    @staticmethod
    def _nested_calls(name, arguments):
        key = "calls" if name == "tool_call" else "tool_uses"
        calls = arguments.get(key)
        if not isinstance(calls, list) or not calls:
            raise InspectionError("invalid_nested_calls")
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                raise InspectionError("invalid_nested_calls")
            nested_name = call.get("name", call.get("recipient_name"))
            nested_args = call.get("arguments", call.get("parameters"))
            if not isinstance(nested_name, str) or not isinstance(nested_args, dict):
                raise InspectionError("invalid_nested_calls")
            arg_key = "arguments" if "arguments" in call else "parameters"
            yield nested_name, nested_args, f"/{key}/{index}/{arg_key}"

    def _effect_error(self, tool_name, name):
        if self.effect_policy == "pattern":
            return ""
        if name in ("terminal", "execute_code"):
            return "opaque_effect"
        policy = self.policies.get(tool_name)
        if not policy:
            return "unsupported_tool"
        return "" if policy[1] else "unverified_external_revision"

    def _validate_call(self, tool_name, arguments):
        # Walk dispatch structure independently of the content budget. A limit
        # in one child cannot hide a malformed or denied sibling.
        if not isinstance(arguments, dict) or not isinstance(tool_name, str):
            raise InspectionError("invalid_tool_payload")
        effect_error = ""
        deadline = time.monotonic() + self.engine.limits.max_inspection_ms / 1000
        stack = [(None, iter(((tool_name, arguments, ""),)))]
        active = set()
        path_groups = []
        while stack:
            if time.monotonic() >= deadline:
                raise InspectionError("call_validation_deadline")
            owner, calls = stack[-1]
            call = next(calls, None)
            if call is None:
                stack.pop()
                active.discard(owner)
                continue
            tool, args, _ = call
            name = tool.rsplit(".", 1)[-1]
            if name in ("tool_call", "parallel"):
                if id(args) in active:
                    raise InspectionError("invalid_nested_calls")
                active.add(id(args))
                stack.append((id(args), self._nested_calls(name, args)))
            elif name == "write_file":
                _validate_path(args["path"])
                if not isinstance(args["content"], str):
                    raise InspectionError("invalid_or_unreadable_tool_payload")
                if active:
                    path_groups.append((args["path"],))
            elif name in ("edit", "edit_file", "patch"):
                mode = args.get("mode", "replace")
                if not isinstance(mode, str):
                    raise InspectionError("invalid_or_unreadable_tool_payload")
                if mode == "replace":
                    _validate_path(args["path"])
                    old, new = args["old_string"], args["new_string"]
                    if (not isinstance(old, str) or not isinstance(new, str)
                            or not isinstance(args.get("replace_all", False), bool)):
                        raise InspectionError("invalid_or_unreadable_tool_payload")
                    if not old or old.isspace():
                        raise InspectionError("unsupported_empty_edit")
                    if active:
                        path_groups.append((args["path"],))
                elif mode == "patch":
                    if not isinstance(args["patch"], str):
                        raise InspectionError("invalid_patch")
                    paths = tuple(path for _, path, _, _ in _patch_files(
                        args["patch"], deadline, timeout_code="call_validation_deadline"))
                    if active or len(paths) > 1:
                        path_groups.append(paths)
                else:
                    raise InspectionError("unsupported_patch_mode")
            elif tool in ("read_file", "search_files"):
                validate_read_arguments(tool, args)
            else:
                effect_error = effect_error or self._effect_error(tool, name)
        if path_groups:
            def check_paths():
                seen = set()
                for paths in path_groups:
                    current = set()
                    for value in paths:
                        _check_deadline(deadline)
                        # File inspection rejects symlinks separately; the
                        # overlap check requires only the host path namespace.
                        path = os.path.abspath(self._namespace_path(value))
                        if path in current:
                            raise InspectionError("duplicate_patch_path")
                        current.add(path)
                    if not seen.isdisjoint(current):
                        raise InspectionError("parallel_file_conflict")
                    seen.update(current)

            try:
                self._file_work(check_paths, deadline)
            except InspectionError as exc:
                if str(exc) in RESOURCE_LIMITS:
                    raise InspectionError("call_validation_deadline") from exc
                raise
        return effect_error

    def inspect_call(self, tool_name, arguments, *, on_match=None, first=False, _depth=0, _deadline=None,
                     _snapshot=None):
        snapshot = _snapshot if _snapshot is not None else self.engine.snapshot
        digest = ""
        effect_error = ""
        hits = []
        try:
            if _depth == 0:
                effect_error = self._validate_call(tool_name, arguments)
            deadline = _deadline if _deadline is not None else time.monotonic() + self.engine.limits.max_inspection_ms / 1000
            if _depth > 16:
                raise InspectionError("tool_nesting_limit")
            check_argument_budget(arguments, self.engine.limits.max_candidate_bytes, deadline)
            digest = payload_digest(tool_name, arguments)
            if time.monotonic() > deadline:
                raise InspectionError("inspection_deadline")
            name = tool_name.rsplit(".", 1)[-1]
            if name in ("tool_call", "parallel"):
                results = []
                for nested_name, nested_args, prefix in self._nested_calls(name, arguments):
                    child = self.inspect_call(nested_name, nested_args, on_match=on_match, first=first,
                                              _depth=_depth + 1, _deadline=deadline, _snapshot=snapshot)
                    results.append(replace(child, hits=tuple(replace(h, field_path=prefix + h.field_path)
                                                             for h in child.hits)))
                paths = [f.path for result in results for f in result.files]
                if len(paths) != len(set(paths)):
                    raise InspectionError("parallel_file_conflict")
                if not first and sum(len(result.hits) for result in results) > self.engine.limits.max_hits:
                    raise InspectionError("inspection_limit")
                errors = [r.error_code for r in results if r.error_code]
                error = next((code for code in errors if code not in RESOURCE_LIMITS),
                             effect_error or (errors[0] if errors else ""))
                if not error and time.monotonic() > deadline and not (first and any(r.hits for r in results)):
                    raise InspectionError("inspection_deadline")
                return ToolInspection(all(r.allowed for r in results), tuple(h for r in results for h in r.hits),
                                      digest, snapshot.digest, tuple(f for r in results for f in r.files),
                                      error)
            candidates = []
            resource_error = ""

            def inspect_text(value, scope, field, *, argument_path=None, targeted_only=False):
                nonlocal resource_error
                if first and hits:
                    return
                try:
                    found = self.engine.inspect(value, scope=scope, field_path=field, deadline=deadline,
                                                first=first, snapshot=snapshot, targeted_only=targeted_only,
                                                argument_tool=tool_name if argument_path is not None else None,
                                                argument_path=argument_path or ())
                except InspectionError as exc:
                    if not first or str(exc) not in RESOURCE_LIMITS:
                        raise
                    resource_error = resource_error or str(exc)
                    return
                if found and on_match is not None:
                    on_match(value)
                hits.extend(found)

            if name in ("write_file", "edit", "edit_file", "patch") and tool_name in snapshot.argument_tools:
                # Explicit argument rules see the submitted text, not the reconstructed file.
                for path, value in _strings(arguments, deadline):
                    inspect_text(value, "tool.arguments", _pointer(path), argument_path=path, targeted_only=True)
                    if first and hits:
                        break
                    if len(hits) > self.engine.limits.max_hits:
                        raise InspectionError("inspection_limit")
            if name == "write_file":
                path, before = self._read_candidate(arguments["path"], deadline)
                candidates = [(path, before, self._preserve_format(before, arguments["content"], operation="write"),
                               "/content", (arguments["content"],))]
            elif name in ("edit", "edit_file", "patch"):
                mode = arguments.get("mode", "replace")
                if mode == "replace":
                    candidates = self._replace(arguments, deadline)
                else:
                    candidates = self._v4a(arguments["patch"], deadline)
            else:
                policy = self.policies.get(tool_name)
                if tool_name in ("read_file", "search_files"):
                    return ToolInspection(True, digest=digest, ruleset_digest=snapshot.digest)
                scopes = ("tool.arguments", "artifact.text") if policy and policy[0] == "document" else ("tool.arguments",)
                for path, value in _strings(arguments, deadline):
                    field = _pointer(path)
                    for scope in scopes:
                        inspect_text(value, scope, field, argument_path=path if scope == "tool.arguments" else None)
                    if first and hits:
                        break
                    if len(hits) > self.engine.limits.max_hits:
                        raise InspectionError("inspection_limit")
                code = self._effect_error(tool_name, name)
                code = code or (resource_error if not hits else "")
                return ToolInspection(not hits and not code, tuple(dict.fromkeys(hits)), digest=digest,
                                      ruleset_digest=snapshot.digest, error_code=code)
            for path, before, after, field, introduced in candidates:
                if first and hits:
                    break
                if after is not None and len(after.encode("utf-8")) > self.engine.limits.max_candidate_bytes:
                    raise InspectionError("candidate_too_large")
                for scope in ("tool.arguments", "artifact.text"):
                    inspect_text(after or "", scope, field)
                for value in introduced:
                    if value and value not in (after or ""):
                        inspect_text(value, "tool.arguments", field)
                if len(hits) > self.engine.limits.max_hits * 2:
                    raise InspectionError("inspection_limit")
            hits = list(dict.fromkeys(hits))
            if len(hits) > self.engine.limits.max_hits:
                raise InspectionError("inspection_limit")
            if time.monotonic() > deadline and not (first and hits):
                raise InspectionError("inspection_deadline")
            error = resource_error if not hits else ""
            return ToolInspection(not hits and not error, tuple(hits), digest, snapshot.digest,
                                  tuple(FileCandidate(path, content_digest(before), after)
                                        for path, before, after, _, _ in candidates),
                                  error_code=error, coverage="predicted_artifact")
        except (InspectionError, OSError, UnicodeError, KeyError, TypeError, ValueError, RecursionError) as exc:
            if isinstance(exc, RecursionError):
                code = "tool_nesting_limit"
            elif isinstance(exc, (OSError, UnicodeError)):
                code = "tool_read_failed"
            else:
                code = str(exc) if isinstance(exc, InspectionError) else "invalid_or_unreadable_tool_payload"
            if effect_error and code in RESOURCE_LIMITS:
                code = effect_error
            matched = tuple(hits[:1]) if first and code in RESOURCE_LIMITS else ()
            return ToolInspection(False, matched, digest=digest, ruleset_digest=snapshot.digest, error_code=code)

    def revalidate(self, result, tool_name, arguments):
        snapshot = self.engine.snapshot
        if not result.allowed or result.ruleset_digest != snapshot.digest:
            return False
        try:
            if result.digest != payload_digest(tool_name, arguments):
                return False
            current = self.inspect_call(tool_name, arguments, _snapshot=snapshot)
            return (self.engine.snapshot is snapshot and current.allowed and
                    current.ruleset_digest == result.ruleset_digest and
                    current.digest == result.digest and current.files == result.files)
        except (OSError, ValueError, TypeError, InspectionError, RecursionError):
            return False
