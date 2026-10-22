import contextvars
from dataclasses import FrozenInstanceError
import threading
import unittest

from guiderails.announcement import Announcement, Announcer


class Channel:
    def __init__(self, supported=True, error=False):
        self.supported = supported
        self.error = error
        self.calls = []
        self.done = threading.Event()

    def supports(self, announcement):
        return self.supported

    def announce(self, announcement):
        self.calls.append(announcement)
        self.done.set()
        if self.error:
            raise RuntimeError("delivery outcome unknown")


class AnnouncerTests(unittest.TestCase):
    def setUp(self):
        self.notice = Announcement("event", "session", "message", "discord")

    def test_frozen_and_first_supported(self):
        with self.assertRaises(FrozenInstanceError):
            self.notice.message = "changed"
        skipped, selected, later = Channel(False), Channel(), Channel()
        Announcer([skipped, selected, later]).submit(self.notice)
        self.assertTrue(selected.done.wait(2))
        self.assertEqual(selected.calls, [self.notice])
        self.assertEqual(skipped.calls, [])
        self.assertEqual(later.calls, [])

    def test_uncertain_delivery_never_fails_over(self):
        later = Channel()
        finished = threading.Event()
        class Failing(Channel):
            def announce(self, announcement):
                try:
                    super().announce(announcement)
                finally:
                    finished.set()
        selected = Failing(error=True)
        Announcer([selected, later]).submit(self.notice)
        self.assertTrue(finished.wait(2))
        self.assertEqual(later.calls, [])

    def test_context_and_active_guard_are_isolated(self):
        scope = contextvars.ContextVar("scope", default="missing")
        seen = []
        done = threading.Event()
        class Capturing(Channel):
            def announce(self, announcement):
                seen.append(("delivery", scope.get()))
                done.set()
        def active():
            seen.append(("active", scope.get()))
            return True
        token = scope.set("submitted")
        try:
            Announcer([Capturing()], is_active=active).submit(self.notice)
            scope.set("changed")
            self.assertTrue(done.wait(2))
            self.assertEqual(seen, [("active", "submitted"), ("delivery", "submitted")])
        finally:
            scope.reset(token)

    def test_coalescing_releases_after_failure(self):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        class Blocking(Channel):
            def announce(self, announcement):
                self.calls.append(announcement)
                if len(self.calls) == 1:
                    entered.set()
                    release.wait(2)
                    raise RuntimeError("unknown")
                second.set()
        channel = Blocking()
        announcer = Announcer([channel])
        announcer.submit(self.notice, coalesce_key="scope")
        self.assertTrue(entered.wait(2))
        announcer.submit(self.notice, coalesce_key="scope")
        self.assertEqual(len(channel.calls), 1)
        release.set()
        from guiderails.announcement import announcer as implementation
        # Wait for the completion callback rather than racing the worker.
        for _ in range(200):
            with implementation._lock:
                if "scope" not in announcer._active:
                    break
            threading.Event().wait(0.01)
        announcer.submit(self.notice, coalesce_key="scope")
        self.assertTrue(second.wait(2))
        self.assertEqual(len(channel.calls), 2)
