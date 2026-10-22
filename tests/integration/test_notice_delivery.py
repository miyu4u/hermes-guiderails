"""Public, best-effort notices at the registered hook boundary."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guiderails.announcement import announcer
from guiderails.announcement.channels.post_message import PostMessageChannel
from guiderails.plugin import register

from .test_profile_integration import Context, local_context, seed


class NoticeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def runtime(self, name):
        home = self.root / name
        seed(home)
        ctx = Context()
        runtime = register(ctx, home=home, current_home=lambda: home,
                           file_context=local_context, channels=[PostMessageChannel(ctx)])
        self.addCleanup(runtime.close)
        return runtime, ctx

    def test_block_warn_and_replay_have_one_concise_notice_per_audit_decision(self):
        runtime, ctx = self.runtime('profile')
        (runtime.home / 'guiderails/config.yaml').write_text(
            'schema_version: 1\nmax_rejections: 1\n', encoding='utf-8')
        posts = []
        ctx.post_message = lambda text: (posts.append(text) or SimpleNamespace(result={'ok': True, 'delivered': True}))
        args = {'path': str(runtime.home / 'private-path'), 'content': 'original'}
        metadata = dict(session_id='session', attempt_id='attempt', tool_call_id='call')
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(announcer, '_executor', executor):
            decision = runtime.pre_tool_call('write_file', args, **metadata)
            self.assertEqual(decision['action'], 'block')
            self.assertEqual(runtime.pre_tool_call('write_file', args, **metadata), decision)
            warned = dict(metadata, tool_call_id='second')
            self.assertIsNone(runtime.pre_tool_call('write_file', args, **warned))
            self.assertIsNone(runtime.pre_tool_call('write_file', args, **warned))
        events = runtime.manager.store.report()
        blocked_event, = [row for row in events if row['decision'] == 'blocked' and row['unit_id'] == 'call']
        warned_event, = [row for row in events if row['decision'] == 'warned_allowed' and row['unit_id'] == 'second']
        self.assertCountEqual(posts, [
            f"Guiderails · BLOCKED · write_file · {blocked_event['event_id'][:8]}",
            f"Guiderails · WARNED · write_file · {warned_event['event_id'][:8]}",
        ])
        self.assertNotIn('private-path', str(posts))
        self.assertNotIn('original', str(posts))
        self.assertIn('Use a specific alternative.', str(runtime.llm_request(
            {'messages': []}, session_id='session', api_request_id='next')))

    def test_same_call_id_in_new_attempt_is_a_new_audit_decision(self):
        runtime, ctx = self.runtime('profile')
        posts = []
        ctx.post_message = lambda text: (posts.append(text) or SimpleNamespace(result={'ok': True}))
        args = {'value': 'original'}
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(announcer, '_executor', executor):
            for attempt in ('first', 'second'):
                metadata = dict(session_id='s', attempt_id=attempt, tool_call_id='same')
                result = runtime.pre_tool_call('fixture_tool', args, **metadata)
                self.assertEqual(result['action'], 'block')
                self.assertEqual(runtime.pre_tool_call('fixture_tool', args, **metadata), result)
        events = [row for row in runtime.manager.store.report()
                  if row['decision'] == 'blocked' and row['unit_id'] == 'same']
        self.assertEqual(len(events), 2)
        self.assertCountEqual(posts, [f"Guiderails · BLOCKED · fixture_tool · {event['event_id'][:8]}"
                                     for event in events])

    def test_timeout_never_resends_edits_or_falls_back_to_viewer(self):
        runtime, ctx = self.runtime('profile')
        posts, viewer, edits = [], [], []
        ctx.emit_to_session = lambda *args: viewer.append(args)
        def post(text):
            posts.append(text)
            return SimpleNamespace(result={'ok': False, 'error': 'timeout'},
                                   edit=lambda *args: edits.append(args))
        ctx.post_message = post
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(announcer, '_executor', executor):
            metadata = dict(session_id='s', attempt_id='first', tool_call_id='one')
            self.assertEqual(runtime.pre_tool_call('fixture_tool', {'value': 'original'}, **metadata)['action'], 'block')
            runtime.pre_tool_call('fixture_tool', {'value': 'original'}, **metadata)
            runtime.llm_request({'messages': []}, session_id='s', api_request_id='next')
            runtime._inspection_bypassed('inspection_deadline', {'session_id': 's'})
        self.assertEqual(len(posts), 2)
        self.assertEqual(sum(text.startswith('Guiderails · BLOCKED · fixture_tool · ') for text in posts), 1)
        self.assertIn('Guiderails · BYPASSED · Inspection budget exceeded', posts)
        self.assertEqual(viewer, [])
        self.assertEqual(edits, [])
        self.assertNotIn('post_api_request', ctx.hooks)

    def test_send_failure_preserves_block_warn_and_model_correction(self):
        runtime, ctx = self.runtime('profile')
        (runtime.home / 'guiderails/config.yaml').write_text(
            'schema_version: 1\nmax_rejections: 1\n', encoding='utf-8')
        posts, viewer = [], []
        def post(text):
            posts.append(text)
            raise RuntimeError('private send failure')
        ctx.post_message = post
        ctx.emit_to_session = lambda *args: viewer.append(args)
        with ThreadPoolExecutor(max_workers=2) as executor, patch.object(announcer, '_executor', executor):
            metadata = dict(session_id='s', attempt_id='first', tool_call_id='one')
            result = runtime.pre_tool_call('fixture_tool', {'value': 'original'}, **metadata)
            self.assertEqual(result['action'], 'block')
            self.assertIsNone(runtime.pre_tool_call('fixture_tool', {'value': 'original'},
                                                    **dict(metadata, tool_call_id='two')))
            self.assertIsNone(runtime.pre_tool_call('fixture_tool', {'value': 'safe'},
                                                    **dict(metadata, tool_call_id='three')))
            correction = runtime.llm_request({'messages': []}, session_id='s', api_request_id='next')
            self.assertIn('Use a specific alternative.', str(correction))
        self.assertEqual(len(posts), 2)
        self.assertEqual(viewer, [])

    def test_parallel_profiles_and_sessions_keep_origin_context(self):
        first, ctx_a = self.runtime('a')
        second, ctx_b = self.runtime('b')
        origin = ContextVar('origin', default='missing')
        posts = []
        lock = threading.Lock()
        def sender(profile):
            def post(text):
                with lock:
                    posts.append((profile, origin.get(), text))
                return SimpleNamespace(result={'ok': True, 'delivered': True})
            return post
        ctx_a.post_message, ctx_b.post_message = sender('a'), sender('b')
        def call(runtime, profile, session):
            token = origin.set(f'{profile}/{session}')
            try:
                return runtime.pre_tool_call('fixture_tool', {'value': 'original'},
                                             session_id=session, attempt_id='attempt', tool_call_id='same')
            finally:
                origin.reset(token)
        with ThreadPoolExecutor(max_workers=4) as executor, patch.object(announcer, '_executor', executor):
            with ThreadPoolExecutor(max_workers=3) as callers:
                futures = [callers.submit(call, runtime, profile, session) for runtime, profile, session in
                           ((first, 'a', 'shared'), (first, 'a', 'other'), (second, 'b', 'shared'))]
                for future in futures:
                    self.assertEqual(future.result(timeout=5)['action'], 'block')
        self.assertEqual(len(posts), 3)
        self.assertCountEqual([(profile, route) for profile, route, _ in posts],
                              [('a', 'a/shared'), ('a', 'a/other'), ('b', 'b/shared')])
        for _, _, text in posts:
            self.assertTrue(text.startswith('Guiderails · BLOCKED · fixture_tool · '))
