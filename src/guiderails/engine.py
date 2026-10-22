"""Validated rule snapshots and bounded matching with original Unicode offsets."""
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import pickle
from queue import Empty, SimpleQueue
import subprocess
import sys
from threading import BoundedSemaphore, Event
import time
import unicodedata

import regex

from .rules import PatternError, parse


class InspectionError(RuntimeError):
    """Inspection error with a stable, content-free code."""


RESOURCE_LIMITS = frozenset({
    'candidate_too_large', 'inspection_deadline', 'inspection_limit',
    'match_count_limit', 'regex_timeout', 'tool_nesting_limit',
})
_RULE_WINDOW = 4
_WORKERS = ThreadPoolExecutor(max_workers=8, thread_name_prefix='guiderails-rule')
_SLOTS = BoundedSemaphore(8)


@dataclass(frozen=True)
class Limits:
    max_candidate_bytes: int = 1048576
    max_rules: int = 1000
    max_pattern_chars: int = 4096
    max_hits: int = 10000
    diff_timeout_ms: float = 1000
    regex_timeout_ms: float = 20
    max_inspection_ms: float = 1000
    compile_timeout_seconds: float = 2

    def __post_init__(self):
        for value in self.__dict__.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise ValueError('invalid_inspection_limit')


@dataclass(frozen=True)
class Hit:
    rule_id: str
    start: int
    end: int
    line: int
    column: int
    instruction: str
    field_path: str = ''
    preexisting: bool = False
    keep: bool = True
    description: str = ''


@dataclass(frozen=True)
class Snapshot:
    digest: str
    document: str
    compiled: tuple
    argument_selectors: tuple[tuple[tuple[str, tuple[str | None, ...]], ...], ...]
    argument_tools: frozenset[str]


def _argument_selectors(rule):
    # None is an array wildcard; an exact selector can still use a literal '*'.
    multiple = 'arguments' in rule
    targets = rule['arguments'] if multiple else [rule['argument']] if 'argument' in rule else []
    return tuple(
        (target['tool'], tuple(None if multiple and part == '*' else part.replace('~1', '/').replace('~0', '~')
                               for part in target['path'][1:].split('/')))
        for target in targets
    )


def _selects_argument(selectors, tool, path, deadline):
    for target_tool, parts in selectors:
        if time.monotonic() >= deadline:
            raise InspectionError('inspection_deadline')
        if target_tool == tool and len(parts) == len(path) and all(
                (isinstance(key, int) if part is None else part == str(key))
                for part, key in zip(parts, path)):
            return True
    return False


def _canonical_units(text):
    """Return NFD code points in canonical order, with their source indices."""
    ordered, marks = [], []
    for index, char in enumerate(text):
        for unit in unicodedata.normalize('NFD', char):
            rank = unicodedata.combining(unit)
            if rank:
                marks.append((rank, unit, index))
            else:
                ordered.extend(sorted(marks, key=lambda item: item[0]))
                marks.clear()
                ordered.append((0, unit, index))
    ordered.extend(sorted(marks, key=lambda item: item[0]))
    return ordered


def normalized(text, nfc, fold=False, *, check=None):
    value = unicodedata.normalize('NFC', text) if nfc else text
    mapping = None
    if check is not None:
        check()
    if value != text:
        pieces, mapping = [], []
        for index, match in enumerate(regex.finditer(r'\X', text)):
            if check is not None and index % 256 == 0:
                check()
            original = match.group()
            piece = unicodedata.normalize('NFC', original)
            pieces.append(piece)
            if piece == original:
                mapping.extend((index, index + 1) for index in range(match.start(), match.end()))
                continue

            # Canonical equivalents have identical ordered decompositions.
            source = _canonical_units(original)
            target = _canonical_units(piece)
            positions = [[] for _ in piece]
            for (_, _, source_index), (_, _, target_index) in zip(source, target):
                positions[target_index].append(match.start() + source_index)
            mapping.extend((min(indices), max(indices) + 1) for indices in positions)
        value = ''.join(pieces)
    if fold and value.casefold() != value:
        old, value = value, value.casefold()
        old_mapping = mapping
        mapping = []
        for index, char in enumerate(old):
            if check is not None and index % 256 == 0:
                check()
            mapping.extend([old_mapping[index] if old_mapping else (index, index + 1)] * len(char.casefold()))
    return value, mapping


class RuleEngine:
    def __init__(self, snapshot, *, limits=None):
        self.limits = limits or Limits()
        self._snapshot = snapshot

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def rules(self):
        return json.loads(self._snapshot.document)['rules']

    @property
    def digest(self):
        return self._snapshot.digest

    def reload(self, text, *, allow_empty=False):
        candidate = type(self).from_yaml(text, limits=self.limits, allow_empty=allow_empty)
        previous = {rule['id']: rule for rule in self.rules}
        for rule in candidate.rules:
            old = previous.get(rule['id'])
            if old is not None and old != rule and rule['revision'] <= old['revision']:
                raise PatternError('nonincreasing_revision')
        self._snapshot = candidate.snapshot
        return self._snapshot

    @classmethod
    def from_yaml(cls, text, *, limits=None, allow_empty=False):
        limits = limits or Limits()
        data = parse(text, allow_empty=allow_empty)
        if len(data['rules']) > limits.max_rules:
            raise PatternError('rules_limit')
        data['rules'].sort(key=lambda r: (-r['priority'], r['id']))
        patterns = [r['matcher'] for r in data['rules'] if r['matcher']['type'] == 'regex']
        if any(m['normalization'] == 'nfc' and unicodedata.normalize('NFC', m['pattern']) != m['pattern'] for m in patterns):
            raise PatternError('regex_pattern_must_be_nfc')
        if any(len(m.get('value', m.get('pattern', ''))) > limits.max_pattern_chars for m in (r['matcher'] for r in data['rules'])):
            raise PatternError('pattern_length_limit')
        if patterns:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join(sys.path))
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('_compile_worker.py'))],
                                        input=json.dumps(patterns).encode(), capture_output=True,
                                        timeout=limits.compile_timeout_seconds, env=env)
            except subprocess.TimeoutExpired as exc:
                raise PatternError('regex_compile_timeout') from exc
            if result.returncode == 2:
                raise PatternError('empty_matching_regex')
            if result.returncode == 3:
                raise PatternError('unsupported_regex_construct')
            if result.returncode:
                raise PatternError('invalid_regex')
            try:
                # regex serializes its compiled bytecode; loading it calls the
                # native pattern constructor, not the Python regex compiler.
                serialized_patterns = pickle.loads(result.stdout)
                if (not isinstance(serialized_patterns, list) or len(serialized_patterns) != len(patterns)
                        or any(not isinstance(pattern, regex.Pattern) for pattern in serialized_patterns)):
                    raise ValueError('invalid compiled pattern count')
            except (pickle.PickleError, ValueError, TypeError, EOFError, AttributeError, ImportError,
                    OverflowError, RuntimeError, IndexError) as exc:
                raise PatternError('invalid_regex') from exc
            pattern_iter = iter(serialized_patterns)
        else:
            pattern_iter = iter(())
        compiled = []
        for rule in data['rules']:
            matcher = rule['matcher']
            if matcher['type'] == 'regex':
                compiled.append(next(pattern_iter))
            else:
                compiled.append(None)
        document = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        selectors = tuple(_argument_selectors(rule) for rule in data['rules'])
        argument_tools = frozenset(tool for rule, targets in zip(data['rules'], selectors)
                                   if rule['enabled'] for tool, _ in targets)
        engine = cls(Snapshot(hashlib.sha256(document.encode()).hexdigest(), document, tuple(compiled),
                              selectors, argument_tools), limits=limits)
        for rule, pattern in zip(data['rules'], compiled):
            try:
                if pattern is not None and pattern.search('', timeout=limits.regex_timeout_ms / 1000):
                    raise PatternError('empty_matching_regex')
                for expected, examples in ((True, rule['tests']['positive']), (False, rule['tests']['negative'])):
                    for example in examples:
                        found = engine._matches(example, rule, pattern, time.monotonic() + limits.max_inspection_ms / 1000)
                        if bool(found) != expected:
                            raise PatternError('corpus_mismatch: ' + rule['id'])
            except (InspectionError, TimeoutError) as exc:
                raise PatternError('unsafe_regex_or_corpus: ' + rule['id']) from exc
        return engine

    def _matches(self, text, rule, pattern, deadline, cache=None, *, first=False, stop=None):
        def check():
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                raise InspectionError('inspection_deadline')

        check()
        matcher = rule['matcher']
        key = (matcher['normalization'] == 'nfc', matcher['type'] == 'match' and not matcher['case_sensitive'])
        if cache is not None and key in cache:
            value, mapping = cache[key]
        else:
            value, mapping = normalized(text, *key, check=check if stop is not None else None)
            if cache is not None:
                cache[key] = (value, mapping)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InspectionError('inspection_deadline')
        spans = []
        if pattern is None:
            needle, _ = normalized(matcher['value'], *key)
            start = value.find(needle)
            while start >= 0:
                spans.append((start, start + len(needle)))
                if first:
                    break
                if len(spans) > self.limits.max_hits:
                    raise InspectionError('match_count_limit')
                if time.monotonic() >= deadline:
                    raise InspectionError('inspection_deadline')
                start = value.find(needle, start + 1)
        else:
            try:
                for match in pattern.finditer(value, overlapped=True, concurrent=True,
                                              timeout=min(remaining, self.limits.regex_timeout_ms / 1000)):
                    if match.start() == match.end():
                        raise InspectionError('empty_matching_regex')
                    spans.append(match.span())
                    if first:
                        break
                    check()
                    if len(spans) > self.limits.max_hits:
                        raise InspectionError('match_count_limit')
            except TimeoutError as exc:
                raise InspectionError('regex_timeout') from exc
        if time.monotonic() > deadline:
            raise InspectionError('inspection_deadline')
        if mapping is None:
            return spans
        original_spans = {}
        for start, end in spans:
            if time.monotonic() > deadline:
                raise InspectionError('inspection_deadline')
            leftmost, rightmost = mapping[start]
            for index in range(start + 1, end):
                left, right = mapping[index]
                if left < leftmost:
                    leftmost = left
                if right > rightmost:
                    rightmost = right
            original_spans[leftmost, rightmost] = None
        return list(original_spans)

    def inspect(self, text, *, scope='assistant.text', field_path='', snapshot=None, deadline=None, first=False,
                argument_tool=None, argument_path=(), targeted_only=False, sample=False):
        start_time = time.monotonic()
        if not isinstance(text, str):
            raise InspectionError('candidate_not_text')
        if len(text) > self.limits.max_candidate_bytes or len(text.encode('utf-8')) > self.limits.max_candidate_bytes:
            raise InspectionError('candidate_too_large')
        deadline = min(deadline if deadline is not None else float('inf'), start_time + self.limits.max_inspection_ms / 1000)
        if start_time >= deadline:
            raise InspectionError('inspection_deadline')
        selected = snapshot or self._snapshot
        rules = json.loads(selected.document)['rules']
        applicable = []
        for rule, pattern, selectors in zip(rules, selected.compiled, selected.argument_selectors):
            if not rule['enabled'] or scope not in rule['scopes']:
                continue
            if selectors:
                if not sample and (argument_tool is None or not _selects_argument(
                        selectors, argument_tool, argument_path, deadline)):
                    continue
            elif targeted_only:
                continue
            applicable.append((rule, pattern))
        if time.monotonic() >= deadline:
            raise InspectionError('inspection_deadline')
        if not applicable:
            return ()
        if first:
            matched = self._first_match(text, applicable, deadline)
            if matched is None:
                return ()
            rule, (start, end) = matched
            return (Hit(rule['id'], start, end, text.count('\n', 0, start) + 1,
                        start - text.rfind('\n', 0, start), rule['announce']['instruction'], field_path,
                        keep=rule.get('keep', True), description=rule['description']),)
        hits, cache, seen = [], {}, set()
        newlines = [-1] + [index for index, char in enumerate(text) if char == '\n']
        for rule, pattern in applicable:
            for start, end in self._matches(text, rule, pattern, deadline, cache):
                if (start, end) in seen:
                    continue
                seen.add((start, end))
                line = bisect_right(newlines, start - 1)
                hits.append(Hit(rule['id'], start, end, line,
                                start - newlines[line - 1], rule['announce']['instruction'], field_path,
                                keep=rule.get('keep', True), description=rule['description']))
                if len(hits) > self.limits.max_hits or time.monotonic() > deadline:
                    raise InspectionError('inspection_limit')
        if time.monotonic() > deadline:
            raise InspectionError('inspection_deadline')
        return tuple(hits)

    def _first_match(self, text, applicable, deadline):
        # Only submit a small window, so early matches leave most rules unscheduled.
        # Running native searches cannot be cancelled by Future.cancel; their timeout
        # remains in force while Python-side work observes stop.
        completed = SimpleQueue()
        pending = {}
        stop = Event()
        rules = iter(applicable)
        next_rule = next(rules, None)
        resource_error = None
        cache = {}

        def run(rule, pattern):
            return self._matches(text, rule, pattern, deadline, cache, first=True, stop=stop)

        def finished(future):
            completed.put(future)
            _SLOTS.release()

        try:
            while next_rule is not None or pending:
                while next_rule is not None and len(pending) < _RULE_WINDOW and completed.empty():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    acquired = _SLOTS.acquire(blocking=False) if pending else _SLOTS.acquire(timeout=remaining)
                    if not acquired:
                        break
                    rule, pattern = next_rule
                    try:
                        future = _WORKERS.submit(run, rule, pattern)
                    except BaseException:
                        _SLOTS.release()
                        raise
                    pending[future] = rule
                    future.add_done_callback(finished)
                    next_rule = next(rules, None)
                try:
                    future = completed.get(timeout=max(0, deadline - time.monotonic()))
                except Empty as exc:
                    raise InspectionError('inspection_deadline') from exc
                rule = pending.pop(future)
                try:
                    spans = future.result()
                except InspectionError as exc:
                    if str(exc) not in RESOURCE_LIMITS:
                        raise
                    resource_error = resource_error or exc
                else:
                    if spans:
                        return rule, spans[0]
            if resource_error:
                raise resource_error
            return None
        finally:
            stop.set()
            for future in pending:
                future.cancel()

    def inspect_edit(self, before, after, *, scope='artifact.text', field_path='', snapshot=None, deadline=None):
        selected = snapshot or self._snapshot
        started = time.monotonic()
        deadline = min(deadline if deadline is not None else float('inf'), started + self.limits.max_inspection_ms / 1000)
        previous = self.inspect(before, scope=scope, field_path=field_path, snapshot=selected, deadline=deadline)
        current = self.inspect(after, scope=scope, field_path=field_path, snapshot=selected, deadline=deadline)
        if max(len(before), len(after)) > 65536:
            raise InspectionError('edit_comparison_limit')
        remaining = min(deadline - time.monotonic(), self.limits.diff_timeout_ms / 1000)
        if remaining <= 0:
            raise InspectionError('inspection_deadline')
        script = ('import difflib,json,sys; a,b=json.load(sys.stdin); '
                  'json.dump(difflib.SequenceMatcher(None,a,b,autojunk=True).get_matching_blocks(),sys.stdout)')
        try:
            result = subprocess.run([sys.executable, '-c', script], input=json.dumps([before, after]),
                                    text=True, capture_output=True, timeout=remaining,
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        except subprocess.TimeoutExpired as exc:
            raise InspectionError('edit_comparison_timeout') from exc
        if result.returncode:
            raise InspectionError('edit_comparison_failed')
        blocks = json.loads(result.stdout)
        old_spans = {(hit.rule_id, hit.start, hit.end) for hit in previous}
        policies = {rule['id']: rule['edit_policy'] for rule in json.loads(selected.document)['rules']}
        result = []
        for hit in current:
            preexisting = False
            if policies[hit.rule_id] == 'introduced':
                for original, changed, size in blocks:
                    if changed <= hit.start and hit.end <= changed + size:
                        offset = original - changed
                        preexisting = (hit.rule_id, hit.start + offset, hit.end + offset) in old_spans
                        break
            result.append(replace(hit, preexisting=preexisting))
        if time.monotonic() > deadline:
            raise InspectionError('inspection_deadline')
        return tuple(result)
