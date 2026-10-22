"""User-visible delivery through the public Hermes plugin API."""

import logging

from ..contracts import Announcement

logger = logging.getLogger(__name__)


class PostMessageChannel:
    def __init__(self, ctx):
        self._ctx = ctx

    def supports(self, announcement: Announcement) -> bool:
        return callable(getattr(self._ctx, 'post_message', None))

    def announce(self, announcement: Announcement) -> None:
        # The worker holds the original routing context. Hermes may complete a
        # timed-out post later, so the handle is never edited, awaited or retried.
        result = self._ctx.post_message(announcement.message).result
        if not result.get('ok') or result.get('delivered') is False:
            logger.debug('Guiderails public notice delivery not confirmed')
