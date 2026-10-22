"""Hook boundary behavior without touching the running Hermes profile."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guiderails.engine import InspectionError
from guiderails.announcement import announcer
from guiderails.announcement.channels.post_message import PostMessageChannel
from guiderails.plugin import register

from .test_profile_integration import Context, create_runtime, inspect, local_context, model, seed


class PreToolCallTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.runtime, self.ctx = create_runtime(self.home)
        self.ctx.target = self.home / 'document.txt'
        self.addCleanup(self.runtime.close)
        self.hook = self.ctx.hooks['pre_tool_call']

    def test_direct_reads_do_not_open_profile_even_with_invalid_policy(self):
        with patch('guiderails.plugin.PatternManager', side_effect=AssertionError('must remain lazy')):
            self.assertIsNone(self.hook('read_file', {'path': 'document.txt'}))
            self.assertIsNone(self.hook('search_files', {'pattern': 'forbidden'}))
            self.assertIsNone(self.hook('read_file', {'path': 'a' * 1048577}))
        self.assertFalse((self.home / 'guiderails').exists())

    def test_management_validation_does_not_initialize_profile(self):
        self.assertEqual(model(self.ctx, 'list', sample=[])['error_code'], 'invalid_management_arguments')
        self.assertEqual(model(self.ctx, 'stats', operation_id='op')['error_code'], 'invalid_management_filters')
        result = self.hook('guiderails_patterns', {'action': 'approve'})
        self.assertEqual(result['message'].splitlines()[0], 'human_approval_command_required')
        self.assertFalse((self.home / 'guiderails').exists())

    def test_profile_guidance_uses_valid_overrides_and_logs_invalid_warning_once(self):
        seed(self.home)
        (self.home / 'guiderails/config.yaml').write_text(
            'schema_version: 1\n'
            'max_rejections: 1\n'
            'guidance_templates:\n'
            '  correction: |-\n'
            '    <Retry {rule_id}> {{details}}\n'
            '    {description} {instruction}\n'
            '    {continuation}\n'
            '  warning: "{unknown}"\n'
            '  invalid_tool_payload: Check the profile tool inputs.\n',
            encoding='utf-8',
        )
        with self.assertLogs('guiderails.guidance', level='ERROR') as logs:
            malformed = self.hook('read_file', [])
            self.assertIn('Check the profile tool inputs.', malformed['message'])
            self.assertFalse((self.home / 'guiderails/state.sqlite3').exists())
            self.assertEqual(inspect(self.ctx, 'original')['action'], 'block')
            request = {'input': [], 'instructions': 'Base'}
            llm_request = self.ctx.hooks['llm_request']
            correction = llm_request(request, session_id='session-1', api_request_id='request-2')
            self.assertIn(
                '<guiderails>\n&lt;Retry test.initial&gt; {details}\n'
                'Reject the specified literal. Use a specific alternative.\n',
                correction['request']['instructions'],
            )
            self.assertIsNone(inspect(self.ctx, 'original', request='request-2', call='call-2'))
            warning = llm_request(request, session_id='session-1', api_request_id='request-3')
            self.assertIn('Guiderails warning (test.initial):', warning['request']['instructions'])
            self.assertIn('How to fix the next call: Use a specific alternative.', warning['request']['instructions'])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("'warning'", logs.output[0])

    def test_observe_allows_calls_when_audit_export_or_storage_fails(self):
        seed(self.home)
        (self.home / 'guiderails' / 'config.yaml').write_text(
            'schema_version: 1\nmode: observe\n', encoding='utf-8')
        self.assertIsNone(inspect(self.ctx, 'original'))
        events = model(self.ctx, 'report')['events']
        self.assertTrue(any(event['event_type'] == 'detected' for event in events))
        self.assertNotIn('original', json.dumps(events))
        with self.assertLogs('guiderails', level='WARNING') as captured, \
                patch.object(self.runtime.manager.audit, 'flush', side_effect=OSError('private runtime error body')):
            self.assertIsNone(inspect(self.ctx, 'original', call='export-failure'))
            report = model(self.ctx, 'report')
            self.assertTrue(report['ok'])
            self.assertTrue(report['audit_pending'])
            self.assertEqual([event['unit_id'] for event in report['events'] if event['event_type'] == 'detected'][-1],
                             'export-failure')
        self.assertNotIn('private runtime error body', '\n'.join(captured.output))
        with patch.object(self.runtime.manager.store, 'record_detection', side_effect=OSError('storage failure')):
            self.assertIsNone(inspect(self.ctx, 'original', call='storage-failure'))
        self.assertEqual(self.runtime.manager.store.outbox_count(), 1)

    def test_runtime_failures_pass_control_back_to_hermes(self):
        with patch('guiderails.plugin.PatternManager', side_effect=RuntimeError('profile unavailable')):
            self.assertIsNone(self.hook('custom_tool', {'text': 'original'}))
        request = {'input': [{'role': 'user', 'content': 'continue'}]}
        with patch.object(self.runtime._continuation, 'rewrite', side_effect=RuntimeError('rewrite failed')):
            self.assertIsNone(self.ctx.hooks['llm_request'](request))

        seed(self.home)
        with patch.object(self.runtime._continuation, 'enqueue', side_effect=RuntimeError('queuing failed')):
            first = inspect(self.ctx, 'original', call='missed-guidance')
            repeated = inspect(self.ctx, 'original', call='missed-guidance')
        self.assertEqual(first, repeated)
        self.assertEqual(first['message'].splitlines()[0], 'guiderails.pattern_detected')
        self.assertNotIn('queuing failed', first['message'])

    def test_seeded_nested_json_and_matching_values(self):
        seed(self.home)
        rng = random.Random(20261124)
        alphabet = ['plain', 'e\u0301', '한글', '👩\u200d💻', '\n', '\x00']
        for index in range(12):
            text = ''.join(rng.choice(alphabet) for _ in range(5))
            payload = {'outer': [None, {'nested': [index, text]}]}
            with self.subTest(seed=20261124, index=index, payload=payload):
                self.assertIsNone(self.hook('custom_tool', payload))
                self.assertIsNone(self.hook('read_file', dict(payload, path='doc')))
                payload['outer'][1]['nested'][1] += ' original'
                result = self.hook('custom_tool', payload, tool_call_id=f'generated-{index}')
                self.assertEqual(result['action'], 'block')
                self.assertEqual(result['message'].splitlines()[0], 'guiderails.pattern_detected')
                self.assertIsNone(self.hook('read_file', dict(payload, path='doc')))

    def test_json_depth_surrogate_and_utf8_byte_boundaries(self):
        seed(self.home)
        for depth in (31, 32):
            value = 'safe'
            for _ in range(depth):
                value = [value]
            payload = {'value': value}
            with self.subTest(depth=depth):
                with patch.object(self.runtime, '_inspection_bypassed') as bypass:
                    self.assertIsNone(self.hook('custom_tool', payload, session_id='nested-session'))
                if depth == 31:
                    bypass.assert_not_called()
                else:
                    bypass.assert_called_once_with('tool_nesting_limit', {'session_id': 'nested-session'})
                self.assertIsNone(self.hook('read_file', dict(payload, path='doc')))

        with patch.object(self.runtime, '_inspection_bypassed') as bypass:
            result = self.hook('custom_tool', {'first': 'original', 'deep': value})
            self.assertEqual(result['message'].splitlines()[0], 'guiderails.pattern_detected')
            result = self.hook('custom_tool', [value])
            self.assertEqual(result['message'].splitlines()[0], 'guiderails.invalid_tool_payload')
            result = self.hook('parallel', {'tool_uses': [{'name': 'custom_tool', 'arguments': [value]}]})
            self.assertEqual(result['message'].splitlines()[0], 'invalid_nested_calls')
            bypass.assert_not_called()

        for index, character in enumerate(('\ud800', '\udfff')):
            payload = {'nested': ['safe', character]}
            with self.subTest(surrogate=index):
                self.assertIsNone(self.hook('custom_tool', payload))
                self.assertEqual(self.hook('read_file', dict(payload, path='doc'))['message'].splitlines()[0],
                                 'guiderails.invalid_tool_payload')

        limit = 1048576
        for delta in (-1, 0, 1):
            # JSON serialization includes the key, quotes and space; é occupies two UTF-8 bytes.
            payload = {'value': 'é' + 'x' * (limit + delta - len('{"value": "é"}'.encode('utf-8')))}
            self.assertEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), limit + delta)
            with self.subTest(bytes=limit + delta):
                for name in ('custom_tool', 'read_file'):
                    args = payload if name == 'custom_tool' else {
                        'path': 'é' + 'x' * (limit + delta - len('{"path": "é"}'.encode('utf-8'))),
                    }
                    with patch.object(self.runtime, '_inspection_bypassed') as bypass:
                        self.assertIsNone(self.hook(name, args))
                    if delta > 0:
                        bypass.assert_called_once_with('candidate_too_large', {})
                    else:
                        bypass.assert_not_called()

    def test_content_budget_does_not_override_argument_or_effect_rejections(self):
        seed(self.home)
        (self.home / 'guiderails' / 'config.yaml').write_text(
            'schema_version: 1\neffect_policy: verified\nlimits:\n  max_candidate_bytes: 1024\n',
            encoding='utf-8')
        with patch.object(self.runtime, '_inspection_bypassed') as bypass:
            for size in (5, 2048):
                text = 'x' * size
                for name, args, code in (
                    ('custom_tool', {'text': text}, 'unsupported_tool'),
                    ('terminal', {'command': text}, 'opaque_effect'),
                    ('write_file', {'content': text, 'path': 42}, 'invalid_or_unreadable_tool_payload'),
                    ('read_file', {'path': text, 'offset': []}, 'guiderails.invalid_tool_payload'),
                    ('parallel', {'tool_uses': [
                        {'name': 'write_file', 'arguments': {'path': str(self.ctx.target), 'content': text}},
                        {'name': 'custom_tool', 'arguments': {}},
                    ]}, 'unsupported_tool'),
                    ('parallel', {'tool_uses': [
                        {'name': 'write_file', 'arguments': {'path': str(self.ctx.target), 'content': text}},
                        {'name': 'write_file', 'arguments': []},
                    ]}, 'invalid_nested_calls'),
                ):
                    with self.subTest(name=name, size=size, code=code):
                        self.assertEqual(self.hook(name, args)['message'].splitlines()[0], code)
            bypass.assert_not_called()
            self.assertIsNone(self.hook('write_file', {'path': str(self.ctx.target), 'content': 'x' * 2048},
                                        session_id='write-session'))
            bypass.assert_called_once_with('candidate_too_large', {'session_id': 'write-session'})

    def test_replace_all_overflow_requests_notice(self):
        seed(self.home)
        (self.home / 'guiderails' / 'config.yaml').write_text(
            'schema_version: 1\nlimits:\n  max_candidate_bytes: 1024\n', encoding='utf-8')
        self.ctx.target.write_bytes(b'x' * 500)
        args = {'path': str(self.ctx.target), 'old_string': 'x', 'new_string': 'é' * 100, 'replace_all': True}
        with patch.object(self.runtime, '_inspection_bypassed') as bypass:
            self.assertIsNone(self.hook('patch', args, session_id='edit-session'))
        bypass.assert_called_once_with('candidate_too_large', {'session_id': 'edit-session'})

    def test_stalled_file_read_times_out_without_blocking_independent_calls(self):
        seed(self.home)
        (self.home / 'guiderails' / 'config.yaml').write_text(
            'schema_version: 1\nlimits:\n  max_inspection_ms: 30\n', encoding='utf-8')
        engine = self.runtime.manager.engine
        entered, release, independent, finished = (threading.Event() for _ in range(4))
        results = {}

        def read(*_):
            entered.set()
            release.wait(5)
            return b'safe'

        def write_call():
            results['write'] = self.hook('write_file', {'path': str(self.ctx.target), 'content': 'clean'},
                                         session_id='slow-session')
            finished.set()

        def other_call():
            results['other'] = self.hook('custom_tool', {'value': 'original'}, tool_call_id='independent')
            independent.set()

        for budget in (3000, 30):
            for event in (entered, release, independent, finished):
                event.clear()
            writer = threading.Thread(target=write_call, daemon=True)
            other = threading.Thread(target=other_call, daemon=True)
            with self.subTest(budget=budget), \
                    patch.object(engine, 'limits', replace(engine.limits, max_inspection_ms=budget)), \
                    patch('guiderails.adapters.read_bytes', side_effect=read), \
                    patch.object(self.runtime, '_inspection_bypassed') as bypass:
                writer.start()
                try:
                    self.assertTrue(entered.wait(1))
                    if budget == 3000:
                        other.start()
                        self.assertTrue(independent.wait(1))
                        self.assertFalse(finished.is_set())
                        self.assertEqual(results['other']['message'].splitlines()[0], 'guiderails.pattern_detected')
                    else:
                        self.assertTrue(finished.wait(1))
                finally:
                    release.set()
                    writer.join(2)
                    if other.ident is not None:
                        other.join(2)
                self.assertIsNone(results['write'])
                if budget == 3000:
                    bypass.assert_not_called()
                else:
                    bypass.assert_called_once_with('inspection_deadline', {'session_id': 'slow-session'})

        for outcome in (OSError('file inaccessible'), b'\xff'):
            with self.subTest(outcome=outcome), \
                    patch('guiderails.adapters.read_bytes',
                          side_effect=outcome if isinstance(outcome, OSError) else None,
                          return_value=outcome), \
                    patch.object(self.runtime, '_inspection_bypassed') as bypass:
                result = self.hook('write_file', {'path': str(self.ctx.target), 'content': 'clean'})
                self.assertIsNone(result)
                bypass.assert_not_called()

    def test_large_and_slow_calls_pass_with_debug_log_and_off_thread_notice(self):
        seed(self.home)
        self.ctx = Context()
        self.runtime = register(self.ctx, home=self.home, current_home=lambda: self.home,
                                file_context=local_context, channels=[PostMessageChannel(self.ctx)])
        self.addCleanup(self.runtime.close)
        self.hook = self.ctx.hooks['pre_tool_call']
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        notices = []

        def post(message):
            notices.append(message)
            entered.set()
            release.wait(2)
            finished.set()
            return SimpleNamespace(result={'ok': True, 'delivered': True})

        self.ctx.post_message = post
        try:
            with self.assertLogs('guiderails.plugin', level='DEBUG') as logged:
                self.assertIsNone(self.hook('custom_tool', {'content': 'private' * 200_000},
                                            session_id='session-a'))
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.is_set(), 'notification must not hold the hook open')
                self.assertIsNone(self.hook('custom_tool', {'content': 'clean'}))
            self.assertIn('candidate_too_large', logged.output[0])
            self.assertNotIn('private', logged.output[0] + notices[0])
        finally:
            release.set()
        self.assertTrue(finished.wait(1))

        events = []
        self.ctx.post_message = lambda _: SimpleNamespace(result={'ok': False, 'error': 'send_failed'})
        self.ctx.emit_to_session = lambda *args: events.append(args)
        engine = self.runtime.manager.engine
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(announcer, '_executor', executor):
            with patch.object(engine, '_matches', side_effect=InspectionError('regex_timeout')):
                with self.assertLogs('guiderails.plugin', level='DEBUG') as logged:
                    self.assertIsNone(self.hook('custom_tool', {'content': 'clean'}, session_id='session-b'))
        self.assertEqual(events, [])
        self.assertIn('regex_timeout', logged.output[0])
        self.assertEqual(model(self.ctx, 'stats')['stats']['detection_count'], 0)
        self.assertEqual(self.hook('custom_tool', {'content': 'original'})['action'], 'block')
