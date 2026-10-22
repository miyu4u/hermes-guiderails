import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from guiderails.adapters import ToolInspector
from guiderails.engine import InspectionError
from guiderails.manager import PatternManager
from guiderails.rules import PatternError
from guiderails.state import StateStore, StorageError


def authorize(principal, action):
    return principal in ('owner', 'model') if action in ('read', 'propose') else principal == 'owner'


def document(*, revision=1, value='금지', enabled=True):
    return json.dumps({
        'schema_version': 1,
        'rules': [{
            'id': 'demo.forbidden', 'revision': revision, 'enabled': enabled,
            'description': 'Flag wording under review.', 'priority': 100,
            'scopes': ['artifact.text'], 'edit_policy': 'whole_result',
            'matcher': {'type': 'match', 'value': value, 'case_sensitive': True, 'normalization': 'nfc'},
            'announce': {'instruction': 'Use a concrete description.'},
            'tests': {'positive': [value + ' 표현'], 'negative': ['검토 결과']},
        }],
    }, ensure_ascii=False)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(folder).resolve()
        self.patterns = self.home / 'guiderails' / 'patterns'
        self.patterns.mkdir(parents=True)

    def open_manager(self, **options):
        return PatternManager(self.home, authorizer=authorize, **options)

    def test_queries_filter_real_detections_without_changing_snapshot(self):
        (self.patterns / 'writing.yaml').write_text(document(), encoding='utf-8')
        with self.open_manager() as manager:
            digest = manager.engine.digest
            hits = manager.engine.inspect('금지 표현', scope='artifact.text')
            for session, operation in [('s1', 'first'), ('s2', 'first'), ('s1', 'second')]:
                manager.store.record_detection({
                    'session_id': session, 'operation_id': operation,
                    'turn_id': 't', 'attempt_id': 'a', 'unit_id': 'u',
                }, hits)

            self.assertEqual([rule['id'] for rule in manager.list_patterns(principal='model')],
                             ['demo.forbidden'])
            self.assertEqual(manager.status(principal='model')['ruleset_digest'], digest)
            self.assertEqual(manager.test('demo.forbidden', principal='model',
                                          samples=['금지 표현', '검토 결과'])['samples'], [True, False])
            self.assertEqual(manager.stats(principal='model', session_id='s1')['detection_count'], 2)
            first, = manager.report(principal='model', session_id='s1', limit=1)
            second, = manager.report(principal='model', session_id='s1', limit=1,
                                     after_sequence=first['sequence'])
            self.assertEqual((first['operation_id'], second['operation_id']), ('first', 'second'))
            self.assertEqual(manager.report(principal='model', operation_id='second'), [second])
            with self.assertRaisesRegex(PatternError, '^invalid_management_arguments$'):
                manager.test('demo.forbidden', principal='model', samples=[1])
            for method, filters in (
                (manager.stats, {'session_id': []}),
                (manager.stats, {'operation_id': 'first'}),
                (manager.report, {'operation_id': 1}),
                (manager.report, {'session_id': ''}),
                (manager.report, {'limit': True}),
                (manager.report, {'after_sequence': -1}),
            ):
                with self.subTest(filters=filters), self.assertRaisesRegex(
                    PatternError, '^invalid_management_filters$',
                ):
                    method(principal='model', **filters)
            self.assertEqual(manager.engine.digest, digest)
            self.assertEqual(manager.engine.inspect('금지 표현', scope='artifact.text'), hits)

    def test_draft_requires_trusted_approval_and_reviewed_digest(self):
        with self.open_manager(provider=lambda *_: json.loads(document())) as manager:
            proposal = manager.generate_proposal('Flag vague wording', principal='model')
            self.assertEqual(list(manager.drafts.iterdir()), [])
            draft = manager.save_proposal(proposal, principal='model')
            self.assertFalse(json.loads(draft.document)['rules'][0]['enabled'])
            self.assertEqual(draft.digest, hashlib.sha256(draft.document.encode()).hexdigest())
            self.assertIsNone(manager.status(principal='model')['ruleset_digest'])
            with self.assertRaisesRegex(InspectionError, '^no_valid_ruleset$'):
                manager.engine
            with self.assertRaises(PermissionError):
                manager.approve(draft.draft_id, expected_digest=draft.digest, principal='model')
            with self.assertRaisesRegex(PatternError, '^approval_digest_mismatch$'):
                manager.approve(draft.draft_id, expected_digest='0' * 64, principal='owner')
            self.assertFalse((self.patterns / 'managed.yaml').exists())
            self.assertEqual(manager.report(principal='model'), [])

            digest = manager.approve(draft.draft_id, expected_digest=draft.digest, principal='owner')
            self.assertTrue(manager.engine.inspect('금지 표현', scope='artifact.text'))
            prepared, committed = manager.report(principal='model')
            self.assertEqual([prepared['event_type'], committed['event_type']],
                             ['pattern_change_prepared', 'pattern_changed'])
            self.assertEqual(prepared['operation_id'], committed['operation_id'])
            self.assertEqual(committed['receipt_ref'], draft.draft_id)
            self.assertEqual(committed['ruleset_digest'], digest)

    def test_approval_and_restart_at_minimum_outbox_capacity(self):
        (self.home / 'guiderails' / 'config.yaml').write_text(
            'schema_version: 1\nlogging: {max_outbox: 2}\n', encoding='utf-8',
        )
        with self.open_manager() as manager:
            draft = manager.save_proposal(document(), principal='model')
            digest = manager.approve(draft.draft_id, expected_digest=draft.digest, principal='owner')
            self.assertTrue(manager.engine.inspect('금지 표현', scope='artifact.text'))
        with self.open_manager() as manager:
            self.assertEqual(manager.engine.digest, digest)
            self.assertTrue(manager.engine.inspect('금지 표현', scope='artifact.text'))
            self.assertEqual([event['event_type'] for event in manager.report(principal='model')],
                             ['pattern_change_prepared', 'pattern_changed'])

    def test_truthy_authorization_does_not_allow_draft_storage(self):
        with PatternManager(self.home, authorizer=lambda *_: 1) as manager:
            with self.assertRaises(PermissionError):
                manager.save_proposal(document(), principal='model')
            self.assertEqual(list(manager.drafts.iterdir()), [])

    def test_invalid_initial_rules_allow_draft_but_reject_activation_until_fixed(self):
        source = self.patterns / 'writing.yaml'
        source.write_bytes(b'\xff')
        with self.open_manager() as manager:
            self.assertEqual(manager.status(principal='model')['error_code'], 'invalid_pattern_source')
            draft = manager.save_proposal(document(), principal='model')
            with self.assertRaisesRegex(PatternError, '^invalid_pattern_source$'):
                manager.approve(draft.draft_id, expected_digest=draft.digest, principal='owner')
            self.assertEqual(source.read_bytes(), b'\xff')
            self.assertIsNone(manager.status(principal='model')['ruleset_digest'])
            source.unlink()
            manager.approve(draft.draft_id, expected_digest=draft.digest, principal='owner')
            self.assertTrue(manager.engine.inspect('금지', scope='artifact.text'))

    def test_malformed_inputs_and_revision_conflicts_preserve_active_rules(self):
        with self.open_manager() as manager:
            digest = manager.apply(document(), principal='owner')
            target = self.patterns / 'managed.yaml'
            original = target.read_bytes()
            draft = manager.save_proposal(document(revision=2), principal='model')
            stored = manager.drafts / (draft.draft_id + '.json')
            stored.write_bytes(b'\xff')
            with self.assertRaisesRegex(PatternError, '^invalid_draft_data$'):
                manager.approve(draft.draft_id, expected_digest=draft.digest, principal='owner')
            stale = manager.save_proposal(document(value='모호'), principal='model')
            with self.assertRaisesRegex(PatternError, '^nonincreasing_revision$'):
                manager.approve(stale.draft_id, expected_digest=stale.digest, principal='owner')
            with self.assertRaisesRegex(PatternError, '^rule_revision_conflict$'):
                manager.set_enabled('demo.forbidden', False, expected_revision=2, principal='owner')
            manual = self.patterns / 'writing.yaml'
            manual.write_text(document(revision=2), encoding='utf-8')
            with self.assertRaisesRegex(PatternError, '^manual_source_conflict$'):
                manager.apply(document(revision=2), principal='owner')
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(manager.engine.digest, digest)
            self.assertEqual(len(manager.report(principal='model')), 2)

            manual.unlink()
            target.write_bytes(b'\xff')
            with self.assertRaisesRegex(PatternError, '^invalid_managed_patterns$'):
                manager.apply(document(revision=2), principal='owner')
            self.assertEqual(target.read_bytes(), b'\xff')
            self.assertEqual(manager.engine.digest, digest)

    def test_reload_keeps_snapshot_on_failure_and_uses_startup_config(self):
        source = self.patterns / 'writing.yaml'
        source.write_text(document(), encoding='utf-8')
        with self.open_manager() as manager:
            digest = manager.engine.digest
            config = self.patterns.parent / 'config.yaml'
            config.write_text('schema_version: 1\nmode: observe\n', encoding='utf-8')
            source.write_text(document(value='모호'), encoding='utf-8')
            with self.assertRaisesRegex(PatternError, '^nonincreasing_revision$'):
                manager.reload(principal='owner')
            self.assertEqual(manager.engine.digest, digest)
            self.assertTrue(manager.engine.inspect('금지', scope='artifact.text'))
            source.write_text(document(revision=2, value='모호'), encoding='utf-8')
            self.assertNotEqual(manager.reload(principal='owner'), digest)
            self.assertFalse(manager.engine.inspect('금지', scope='artifact.text'))
            self.assertTrue(manager.engine.inspect('모호', scope='artifact.text'))
            self.assertEqual(manager.status(principal='owner')['mode'], 'strict')
        with self.open_manager() as reopened:
            self.assertEqual(reopened.status(principal='owner')['mode'], 'observe')

    def test_invalid_multi_selector_reload_preserves_digest_and_active_targets(self):
        source = self.patterns / 'writing.yaml'
        data = json.loads(document())
        entry, = data['rules']
        target = {'tool': 'docs.update', 'path': '/changes/*/content'}
        entry.update(scopes=['tool.arguments'], arguments=[target])
        source.write_text(json.dumps(data), encoding='utf-8')
        with self.open_manager() as manager:
            previous = manager.engine.snapshot
            self.assertEqual(manager.test('demo.forbidden', principal='model',
                                          samples=['금지 표현', '검토 결과'])['samples'], [True, False])
            entry.update(revision=2, arguments=[target, target.copy()])
            source.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(PatternError, '^invalid_pattern_schema$'):
                manager.reload(principal='owner')
            self.assertIs(manager.engine.snapshot, previous)
            result = ToolInspector(manager.engine).inspect_call(
                'docs.update', {'changes': [{'content': '정상'}, {'content': '금지'}]})
            self.assertEqual(result.ruleset_digest, previous.digest)
            self.assertEqual([h.field_path for h in result.hits], ['/changes/1/content'])

    def test_restart_rejects_changed_or_older_revision_and_allows_recovery(self):
        source = self.patterns / 'writing.yaml'
        source.write_text(document(revision=2), encoding='utf-8')
        with self.open_manager() as manager:
            digest = manager.engine.digest

        for stale in (document(revision=2, value='모호'), document(revision=1)):
            source.write_text(stale, encoding='utf-8')
            with self.open_manager() as manager:
                self.assertEqual(manager.status(principal='owner')['error_code'], 'nonincreasing_revision')
                with self.assertRaisesRegex(InspectionError, '^no_valid_ruleset$'):
                    manager.engine
                with self.assertRaisesRegex(PatternError, '^nonincreasing_revision$'):
                    manager.reload(principal='owner')

        source.write_text(document(revision=2), encoding='utf-8')
        with self.open_manager() as manager:
            self.assertEqual(manager.engine.digest, digest)
            source.write_text(document(revision=3, value='모호'), encoding='utf-8')
            revised = manager.reload(principal='owner')
            self.assertNotEqual(revised, digest)
        with self.open_manager() as manager:
            self.assertEqual(manager.engine.digest, revised)
            self.assertTrue(manager.engine.inspect('모호', scope='artifact.text'))

    def test_removal_and_reintroduction_preserve_revision_history(self):
        source = self.patterns / 'writing.yaml'
        first = json.loads(document())['rules'][0]
        second = dict(first, id='demo.other')
        both = {'schema_version': 1, 'rules': [first, second]}
        source.write_text(json.dumps(both, ensure_ascii=False), encoding='utf-8')
        with self.open_manager() as manager:
            both['rules'] = [second]
            source.write_text(json.dumps(both, ensure_ascii=False), encoding='utf-8')
            removed_digest = manager.reload(principal='owner')
        both['rules'] = [dict(first, description='Changed wording.'), second]
        source.write_text(json.dumps(both, ensure_ascii=False), encoding='utf-8')
        with self.open_manager() as manager:
            self.assertEqual(manager.status(principal='owner')['error_code'], 'nonincreasing_revision')
            both['rules'][0]['revision'] = 2
            source.write_text(json.dumps(both, ensure_ascii=False), encoding='utf-8')
            self.assertNotEqual(manager.reload(principal='owner'), removed_digest)
        with self.open_manager() as manager:
            self.assertEqual({rule['id'] for rule in manager.list_patterns(principal='owner')},
                             {'demo.forbidden', 'demo.other'})

    def test_approval_and_reload_share_persisted_history(self):
        source = self.patterns / 'writing.yaml'
        source.write_text(document(revision=2), encoding='utf-8')
        with self.open_manager() as manager:
            digest = manager.engine.digest
            source.unlink()
            stale = manager.save_proposal(document(revision=2, value='모호'), principal='model')
            with self.assertRaisesRegex(PatternError, '^nonincreasing_revision$'):
                manager.approve(stale.draft_id, expected_digest=stale.digest, principal='owner')
            self.assertFalse((self.patterns / 'managed.yaml').exists())
            self.assertEqual(manager.engine.digest, digest)
            valid = manager.save_proposal(document(revision=3, value='모호'), principal='model')
            approved = manager.approve(valid.draft_id, expected_digest=valid.digest, principal='owner')
        with self.open_manager() as manager:
            self.assertEqual(manager.engine.digest, approved)
            (self.patterns / 'managed.yaml').write_text(document(revision=3), encoding='utf-8')
            with self.assertRaisesRegex(PatternError, '^nonincreasing_revision$'):
                manager.reload(principal='owner')
            self.assertEqual(manager.engine.digest, approved)
        with self.open_manager() as manager:
            self.assertEqual(manager.status(principal='owner')['error_code'], 'nonincreasing_revision')

    def test_revision_history_is_scoped_to_profile(self):
        source = self.patterns / 'writing.yaml'
        source.write_text(document(revision=2), encoding='utf-8')
        with self.open_manager(profile='one') as manager:
            digest = manager.engine.digest
        source.write_text(document(value='모호'), encoding='utf-8')
        with self.open_manager(profile='two') as manager:
            self.assertTrue(manager.engine.inspect('모호', scope='artifact.text'))
        with self.open_manager(profile='one') as manager:
            self.assertEqual(manager.status(principal='owner')['error_code'], 'nonincreasing_revision')
            source.write_text(document(revision=2), encoding='utf-8')
            self.assertEqual(manager.reload(principal='owner'), digest)

    def test_failed_reload_and_approval_do_not_advance_revision_history(self):
        source = self.patterns / 'writing.yaml'
        source.write_text(document(), encoding='utf-8')
        with self.open_manager() as manager:
            original = manager.engine.digest
            source.write_text(document(revision=2, value='모호'), encoding='utf-8')
            event = manager.store._event

            def fail_reload(event_type, *args, **kwargs):
                if event_type == 'rules_reloaded':
                    raise StorageError('injected_failure')
                return event(event_type, *args, **kwargs)

            with patch.object(manager.store, '_event', side_effect=fail_reload):
                with self.assertRaisesRegex(StorageError, '^injected_failure$'):
                    manager.reload(principal='owner')
            self.assertEqual(manager.engine.digest, original)
            source.write_text(document(), encoding='utf-8')
            self.assertEqual(manager.reload(principal='owner'), original)
            source.unlink()

            def fail_commit(event_type, *args, **kwargs):
                if event_type == 'pattern_changed':
                    raise StorageError('injected_failure')
                return event(event_type, *args, **kwargs)

            with patch.object(manager.store, '_event', side_effect=fail_commit):
                with self.assertRaisesRegex(StorageError, '^injected_failure$'):
                    manager.apply(document(revision=2, value='모호'), principal='owner')
            self.assertFalse((self.patterns / 'managed.yaml').exists())
            self.assertEqual(manager.engine.digest, original)
            source.write_text(document(), encoding='utf-8')
        with self.open_manager() as manager:
            self.assertEqual(manager.engine.digest, original)

    def test_enabling_changes_managed_revision_and_effect(self):
        with self.open_manager() as manager:
            manager.apply(document(enabled=False), principal='owner')
            self.assertFalse(manager.engine.inspect('금지', scope='artifact.text'))
            manager.set_enabled('demo.forbidden', True, expected_revision=1, principal='owner')
            rule, = manager.list_patterns(principal='model')
            self.assertEqual(rule['revision'], 2)
            self.assertTrue(manager.engine.inspect('금지', scope='artifact.text'))
            with self.assertRaisesRegex(PatternError, '^rule_revision_conflict$'):
                manager.set_enabled('demo.forbidden', False, expected_revision=1, principal='owner')

    def test_storage_failure_after_prepare_restores_exact_original(self):
        target = self.patterns / 'managed.yaml'
        original = ('# local comment\r\n' + document() + '\r\n').encode()
        target.write_bytes(original)
        with self.open_manager() as manager:
            digest = manager.engine.digest
            with patch.object(manager, '_commit_change', side_effect=StorageError('injected_failure')):
                with self.assertRaisesRegex(StorageError, '^injected_failure$'):
                    manager.apply(document(revision=2, value='모호'), principal='owner')
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(manager.engine.digest, digest)
            self.assertEqual([event['event_type'] for event in manager.report(principal='model')],
                             ['pattern_change_prepared'])
        with self.open_manager() as reopened:
            self.assertEqual(reopened.engine.digest, digest)
            self.assertTrue(reopened.engine.inspect('금지', scope='artifact.text'))

    def test_restart_commits_prepared_change_if_replaced_bytes_are_present(self):
        with self.open_manager() as manager:
            draft = manager.save_proposal(document(), principal='model')

        # Exit between durable file replacement and commit, bypassing exception rollback.
        child = """
import os
import sys
from guiderails.manager import PatternManager

home, draft_id, digest = sys.argv[1:]
with PatternManager(home, authorizer=lambda *_: True) as manager:
    manager._commit_change = lambda *args: os._exit(73)
    manager.approve(draft_id, expected_digest=digest, principal='owner')
"""
        result = subprocess.run([sys.executable, '-B', '-c', child, os.fspath(self.home),
                                 draft.draft_id, draft.digest], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        with self.open_manager() as manager:
            self.assertTrue(manager.engine.inspect('금지', scope='artifact.text'))
            prepared, changed = manager.report(principal='model')
            self.assertEqual(changed['event_type'], 'pattern_changed')
            self.assertEqual(changed['operation_id'], prepared['operation_id'])
            self.assertEqual(changed['ruleset_digest'], manager.engine.digest)
            manager.audit.flush()
            event_ids = [prepared['event_id'], changed['event_id']]
        with self.open_manager() as manager:
            manager.audit.flush()
            audit = manager.root / 'audit' / 'events.jsonl'
            self.assertEqual([json.loads(line)['event_id'] for line in audit.read_text().splitlines()], event_ids)
        (self.patterns / 'managed.yaml').write_text(document(value='모호'), encoding='utf-8')
        with self.open_manager() as manager:
            self.assertEqual(manager.status(principal='owner')['error_code'], 'nonincreasing_revision')

    def test_initialization_failure_closes_open_storage(self):
        for target, error in (
            ('guiderails.manager.AuditWriter', StorageError('audit_fixture')),
            ('guiderails.manager.PatternManager._load', OSError('load_fixture')),
        ):
            with self.subTest(target=target):
                store = StateStore(self.home / 'guiderails' / 'state.sqlite3')
                with patch('guiderails.manager.StateStore', return_value=store), patch(target, side_effect=error):
                    with self.assertRaises(type(error)):
                        self.open_manager()
                with self.assertRaises(sqlite3.ProgrammingError):
                    store.db.execute('SELECT 1')
