"""Non-blocking, best-effort announcement dispatch."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import logging
import threading
from typing import Callable, Hashable, Iterable

from .contracts import Announcement, IAnnounceChannel

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="guiderails-announce")
_slots = threading.BoundedSemaphore(16)
_lock = threading.RLock()
_waiting = deque()


def _drain():
    """Schedule waiting work; caller holds _lock."""
    while _waiting and _slots.acquire(blocking=False):
        work, finish = _waiting.popleft()

        def completed(_future=None, finish=finish):
            with _lock:
                finish()
                _slots.release()
                _drain()

        try:
            future = _executor.submit(work)
        except Exception:
            # Delivery is best effort; do not run it on the submitting thread.
            finish()
            _slots.release()
            logger.debug("Announcement worker unavailable", exc_info=True)
        else:
            future.add_done_callback(completed)


class Announcer:
    def __init__(
        self,
        channels: Iterable[IAnnounceChannel],
        *,
        is_active: Callable[[], bool] = lambda: True,
    ):
        self._channels = tuple(channels)
        self._is_active = is_active
        self._active: set[Hashable] = set()

    def submit(self, announcement: Announcement, *, coalesce_key: Hashable | None = None) -> None:
        """Queue delivery, coalescing only matching work currently in flight.

        Keys are scoped to this announcer (normally one runtime). A None key
        disables coalescing. Context is captured separately for each submission.
        """
        try:
            context = copy_context()

            def deliver():
                try:
                    if not self._is_active():
                        return
                    for channel in self._channels:
                        # Fail closed on uncertain capability; never try a later
                        # channel after an exception from either operation.
                        if channel.supports(announcement):
                            channel.announce(announcement)
                            return
                except Exception:
                    logger.debug("Announcement delivery failed", exc_info=True)

            def finish():
                if coalesce_key is not None:
                    self._active.discard(coalesce_key)

            with _lock:
                if coalesce_key is not None:
                    if coalesce_key in self._active:
                        return
                    self._active.add(coalesce_key)
                try:
                    _waiting.append((lambda: context.run(deliver), finish))
                    _drain()
                except Exception:
                    finish()
                    raise
        except Exception:
            logger.debug("Announcement submission failed", exc_info=True)
