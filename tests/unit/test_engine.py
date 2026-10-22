import json
import importlib
import random
import unittest
from unittest.mock import patch
import subprocess

import regex

from guiderails.engine import InspectionError, Limits, RuleEngine
from guiderails.rules import PatternError


def rule(rule_id='demo.rule', **changes):
    value = {
        'id': rule_id, 'revision': 1, 'enabled': True, 'description': 'rule',
        'priority': 100, 'scopes': ['artifact.text', 'tool.arguments'],
        'matcher': {'type': 'match', 'value': '금지', 'case_sensitive': True, 'normalization': 'nfc'},
        'edit_policy': 'whole_result', 'announce': {'instruction': '수정하세요.'},
        'tests': {'positive': ['금지'], 'negative': ['정상']},
    }
    value.update(changes)
    return value


def document(*rules):
    return json.dumps({'schema_version': 1, 'rules': rules}, ensure_ascii=False)


class EngineTests(unittest.TestCase):
    def test_argument_selectors_require_exact_scope_and_valid_pointer(self):
        target = {'tool': 'patch', 'path': '/new_string'}
        invalid = [
            rule(argument=target),
            rule(scopes=['tool.arguments'], argument={'tool': '', 'path': '/new_string'}),
            rule(scopes=['tool.arguments'], argument={'tool': 'patch'}),
            rule(scopes=['tool.arguments'], argument={**target, 'path': 'new_string'}),
            rule(scopes=['tool.arguments'], argument={**target, 'path': '/new~2string'}),
        ]
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(PatternError):
                RuleEngine.from_yaml(document(entry))

    def test_multi_selector_schema_rejects_empty_duplicate_and_mixed_forms(self):
        target = {'tool': 'docs.update', 'path': '/changes/*/content'}

        def targeted(**fields):
            return rule(scopes=['tool.arguments'], **fields)

        for entry in (
            rule(arguments=[target]),
            targeted(arguments=[]),
            targeted(arguments=target),
            targeted(arguments=[target, target.copy()]),
            targeted(arguments=[target], argument=target),
            targeted(arguments=[{**target, 'path': '/broken~2path'}]),
            targeted(arguments=[{**target, 'extra': True}]),
        ):
            with self.subTest(entry=entry), self.assertRaisesRegex(PatternError, '^invalid_pattern_schema$'):
                RuleEngine.from_yaml(document(entry))

    def test_keep_defaults_and_is_attributed_to_the_inspected_snapshot(self):
        engine = RuleEngine.from_yaml(document(rule()))
        before = engine.snapshot
        engine.reload(document(rule(revision=2, keep=False, announce={'instruction': 'new'})))
        for first in (True, False):
            original, = engine.inspect('금지', scope='artifact.text', snapshot=before, first=first)
            changed, = engine.inspect('금지', scope='artifact.text', first=first)
            self.assertEqual((original.keep, original.instruction), (True, '수정하세요.'))
            self.assertEqual((changed.keep, changed.instruction), (False, 'new'))
        self.assertTrue(RuleEngine.from_yaml(document(rule(keep=True))).inspect('금지', scope='artifact.text')[0].keep)
        for invalid in ('false', 0, None):
            with self.subTest(value=invalid), self.assertRaises(PatternError):
                engine.reload(document(rule(revision=3, keep=invalid)))
        self.assertFalse(engine.inspect('금지', scope='artifact.text')[0].keep)

    def test_original_unicode_positions_and_field_path(self):
        folded = rule(matcher={'type': 'match', 'value': 'STRASSE', 'case_sensitive': False, 'normalization': 'nfc'},
                      tests={'positive': ['Straße'], 'negative': ['road']})
        engine = RuleEngine.from_yaml(document(folded))
        hit, = engine.inspect('앞\nStraße!', scope='artifact.text', field_path='content')
        self.assertEqual((hit.start, hit.end, hit.line, hit.column, hit.field_path), (2, 8, 2, 1, 'content'))
        regex_rule = rule(matcher={'type': 'regex', 'pattern': '^가.+끝$', 'flags': ['MULTILINE', 'DOTALL'], 'normalization': 'nfc'},
                          tests={'positive': ['가운데끝'], 'negative': ['정상']})
        hit, = RuleEngine.from_yaml(document(regex_rule)).inspect('가\n끝', scope='artifact.text')
        self.assertEqual((hit.start, hit.end), (0, 4))

    def test_unicode_original_spans_remain_precise_when_other_text_normalizes(self):
        cases = [
            ('👩', True, '👩\u200d💻 e\u0301', (0, 1)),
            ('💻', True, '👩\u200d💻 e\u0301', (2, 3)),
            ('é', True, 'x e\u0301!', (2, 4)),
            ('ss', False, 'x ß e\u0301', (2, 3)),
            ('à', True, 'a\u0315\u0300', (0, 3)),
            ('\u0315', True, 'a\u0315\u0300', (1, 2)),
        ]
        for needle, sensitive, text, span in cases:
            with self.subTest(needle=needle, text=text):
                matcher = {'type': 'match', 'value': needle, 'case_sensitive': sensitive, 'normalization': 'nfc'}
                engine = RuleEngine.from_yaml(document(rule(
                    matcher=matcher, tests={'positive': [needle], 'negative': ['unrelated']})))
                hit, = engine.inspect(text, scope='artifact.text')
                self.assertEqual((hit.start, hit.end), span)

    def test_generated_unicode_suffix_does_not_change_original_positions(self):
        rng = random.Random(20261124)
        matcher = {'type': 'match', 'value': '👩', 'case_sensitive': True, 'normalization': 'nfc'}
        engine = RuleEngine.from_yaml(document(rule(
            matcher=matcher, tests={'positive': ['👩'], 'negative': ['unrelated']})))
        prefixes = ['', '앞 ', '앞\n', 'é\nx ', 'e\u0301\n']
        suffixes = ['é', 'e\u0301', '가', '가', '👩\u200d💻', '\n', 'ß']
        for index in range(300):
            prefix = rng.choice(prefixes)
            text = prefix + '👩\u200d💻 ' + ''.join(rng.choice(suffixes) for _ in range(4))
            with self.subTest(seed=20261124, index=index, text=text):
                hit = engine.inspect(text, scope='artifact.text')[0]
                self.assertEqual((hit.start, hit.end, hit.line, hit.column),
                                 (len(prefix), len(prefix) + 1, prefix.count('\n') + 1,
                                  len(prefix.rsplit('\n', 1)[-1]) + 1))

    def test_order_scopes_disabled_and_duplicate_spans(self):
        high = rule('z.high', priority=200)
        first = rule('a.first', priority=100)
        second = rule('b.second', priority=100,
                      matcher={'type': 'match', 'value': '지', 'case_sensitive': True, 'normalization': 'none'},
                      tests={'positive': ['지'], 'negative': ['금']})
        disabled = rule('x.disabled', priority=999, enabled=False)
        engine = RuleEngine.from_yaml(document(first, second, high, disabled))
        hits = engine.inspect('금지', scope='artifact.text')
        self.assertEqual([(h.rule_id, h.start, h.end) for h in hits], [('z.high', 0, 2), ('b.second', 1, 2)])
        self.assertEqual(engine.inspect('금지', scope='tool.result'), ())
        self.assertEqual([r['id'] for r in engine.rules], ['x.disabled', 'z.high', 'a.first', 'b.second'])

    def test_invalid_schema_corpus_and_yaml(self):
        invalid = [
            document(rule(extra=True)), document(rule(revision=True)),
            document(rule(), rule()), document(rule(tests={'positive': ['정상'], 'negative': ['금지']})),
            document(rule(enabled=False, tests={'positive': ['정상'], 'negative': ['금지']})),
            'schema_version: 1\nschema_version: 1\nrules: []',
            'schema_version: 1\nrules: &rules [*rules]',
            'schema_version: 1\nrules: !unsafe []',
            document(rule(matcher={'type': 'regex', 'pattern': 'a*', 'flags': [], 'normalization': 'none'},
                          tests={'positive': ['a'], 'negative': ['b']})),
            document(rule(matcher={'type': 'regex', 'pattern': '[', 'flags': [], 'normalization': 'none'})),
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(PatternError):
                RuleEngine.from_yaml(text)

    def test_failed_reload_preserves_snapshot_digest_and_inspection(self):
        engine = RuleEngine.from_yaml(document(rule()))
        original, hits = engine.snapshot, engine.inspect('금지', scope='artifact.text')
        changed = rule(description='changed')
        for invalid in ('rules: []', document(changed),
                        document(rule(tests={'positive': ['정상'], 'negative': ['금지']}))):
            with self.subTest(invalid=invalid), self.assertRaises(PatternError):
                engine.reload(invalid)
            self.assertIs(engine.snapshot, original)
            self.assertEqual(engine.digest, original.digest)
            self.assertEqual(engine.inspect('금지', scope='artifact.text'), hits)
        changed['revision'] = 2
        engine.reload(document(changed))
        self.assertNotEqual(engine.digest, original.digest)
        self.assertEqual(engine.inspect('금지', scope='artifact.text', snapshot=original), hits)

    def test_canonical_digest_ignores_rule_input_order(self):
        a, b = rule('a.one'), rule('b.two', priority=10)
        self.assertEqual(RuleEngine.from_yaml(document(a, b)).digest,
                         RuleEngine.from_yaml(document(b, a)).digest)

    def test_reload_keeps_pinned_snapshot_policy_isolated(self):
        engine = RuleEngine.from_yaml(document(rule()))
        original = engine.snapshot
        replacement = rule(
            revision=2,
            matcher={'type': 'match', 'value': '새규칙', 'case_sensitive': True, 'normalization': 'nfc'},
            tests={'positive': ['새규칙'], 'negative': ['금지']},
        )
        engine.reload(document(replacement))
        text = '금지 새규칙'
        self.assertEqual([(hit.start, hit.end) for hit in engine.inspect(text, scope='artifact.text')], [(3, 6)])
        self.assertEqual([(hit.start, hit.end) for hit in engine.inspect(text, scope='artifact.text', snapshot=original)],
                         [(0, 2)])

    def test_candidate_limit_counts_utf8_bytes_at_boundary(self):
        engine = RuleEngine.from_yaml(document(rule()), limits=Limits(max_candidate_bytes=6))
        self.assertEqual(len(engine.inspect('금지', scope='artifact.text')), 1)
        with self.assertRaises(InspectionError):
            engine.inspect('금지a', scope='artifact.text')

    def test_limits_and_compile_timeout_fail_closed(self):
        with self.assertRaises(PatternError):
            RuleEngine.from_yaml(document(rule()), limits=Limits(max_pattern_chars=1))
        engine = RuleEngine.from_yaml(document(rule()), limits=Limits(max_candidate_bytes=20, max_hits=2))
        with self.assertRaises(InspectionError):
            engine.inspect('x' * 21, scope='artifact.text')
        with self.assertRaises(InspectionError):
            engine.inspect('금지' * 3, scope='artifact.text')
        regex_rule = rule(matcher={'type': 'regex', 'pattern': '금지', 'flags': [], 'normalization': 'none'})
        with patch('guiderails.engine.subprocess.run', side_effect=subprocess.TimeoutExpired('compile', 2)):
            with self.assertRaisesRegex(PatternError, 'regex_compile_timeout'):
                RuleEngine.from_yaml(document(regex_rule))

    def test_regex_validation_rejects_contextual_empty_matches_before_reload(self):
        engine = RuleEngine.from_yaml(document(rule()))
        original = engine.snapshot
        examples = {'positive': ['a'], 'negative': ['x']}
        for expression, error in (
            ('a|(?=z)', 'unsupported_regex_construct'),
            ('a|b*', 'empty_matching_regex'),
            (r'a\K', 'unsupported_regex_construct'),
            ('a(?=b)', 'unsupported_regex_construct'),
            ('(a)\\1', 'unsupported_regex_construct'),
            ('a{e<=1}', 'unsupported_regex_construct'),
        ):
            candidate = rule(matcher={'type': 'regex', 'pattern': expression, 'flags': [], 'normalization': 'none'},
                             tests=examples, revision=2)
            with self.subTest(expression=expression), self.assertRaisesRegex(PatternError, error):
                engine.reload(document(candidate))
            self.assertIs(engine.snapshot, original)
            self.assertEqual(len(engine.inspect('금지', scope='artifact.text')), 1)

    def test_worker_compiled_patterns_are_used_without_parent_recompilation(self):
        first = rule('z.first', priority=1,
                     matcher={'type': 'regex', 'pattern': r'^(?:ab|c[de]+)$',
                              'flags': ['IGNORECASE'], 'normalization': 'none'},
                     tests={'positive': ['AB', 'cde'], 'negative': ['x']})
        second = rule('a.second', priority=200,
                      matcher={'type': 'regex', 'pattern': r'\d{2,3}',
                               'flags': [], 'normalization': 'none'},
                      tests={'positive': ['12'], 'negative': ['x']})
        compiler_module = importlib.import_module(regex.compile.__module__)
        with patch.object(regex, 'compile', side_effect=AssertionError('parent compile')), \
             patch.object(compiler_module, '_compile', side_effect=AssertionError('parent internal compile')):
            engine = RuleEngine.from_yaml(document(first, rule('m.literal'), second))
            self.assertEqual([(h.rule_id, h.start, h.end) for h in engine.inspect('12', scope='artifact.text')],
                             [('a.second', 0, 2)])
            self.assertEqual([h.rule_id for h in engine.inspect('AB', scope='artifact.text')], ['z.first'])
            self.assertEqual([h.rule_id for h in engine.inspect('금지', scope='artifact.text')], ['m.literal'])

    def test_real_worker_timeout_rejects_candidate_without_changing_snapshot(self):
        limits = Limits(compile_timeout_seconds=0.000000001)
        engine = RuleEngine.from_yaml(document(rule()), limits=limits)
        original = engine.snapshot
        regex_rule = rule(revision=2,
                          matcher={'type': 'regex', 'pattern': '금지', 'flags': [], 'normalization': 'none'})
        with self.assertRaisesRegex(PatternError, 'regex_compile_timeout'):
            engine.reload(document(regex_rule))
        self.assertIs(engine.snapshot, original)
        self.assertEqual(len(engine.inspect('금지', scope='artifact.text')), 1)

    def test_edit_marks_old_and_new_matches(self):
        engine = RuleEngine.from_yaml(document(rule(edit_policy='introduced')))
        hits = engine.inspect_edit('금지 기존', '앞\n금지 기존\n금지')
        self.assertEqual([(h.start, h.preexisting) for h in hits], [(2, True), (8, False)])
