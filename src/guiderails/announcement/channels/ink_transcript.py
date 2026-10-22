"""Compatibility delivery for Hermes Ink via its native transcript notice."""

import logging

from ..contracts import Announcement

logger = logging.getLogger(__name__)


class InkTranscriptChannel:
    def __init__(self, ctx, *, home):
        self._ctx = ctx
        self._home = home

    def supports(self, announcement: Announcement) -> bool:
        surface = getattr(self._ctx, 'session_surface', None)
        if not callable(surface) or surface() != 'tui':
            return False
        try:
            from gateway import plugin_messages
            from gateway.session_context import bound_session_env, bound_session_key
            from hermes_cli.plugin_messages import _gateway_loop
            from tui_gateway.contracts.events import NotificationShowPayload
        except ImportError:
            return False
        return (
            'detail' in NotificationShowPayload.__annotations__
            and callable(getattr(plugin_messages, 'resolve', None))
            and callable(getattr(plugin_messages, 'bound_route', None))
            and getattr(self._ctx, 'plugin_id', None) is not None
            and bool(bound_session_key())
            and bound_session_env('HERMES_SESSION_ID') == announcement.session_id
            and _gateway_loop()[0] is not None
        )

    def announce(self, announcement: Announcement) -> None:
        from gateway import plugin_messages
        from gateway.session_context import bound_session_env, bound_session_key
        from hermes_cli.plugin_messages import _gateway_loop

        runner, _ = _gateway_loop()
        session_key = bound_session_key()
        if runner is None or not session_key:
            return
        target, _ = plugin_messages.resolve(
            runner, session_key=session_key, route=plugin_messages.bound_route(),
            plugin_home=self._home, plugin_id=self._ctx.plugin_id,
        )
        if (target is None or target.live is None or target.platform != 'local'
                or target.chat_id != bound_session_env('HERMES_SESSION_CHAT_ID')):
            return
        key = f'guiderails:{announcement.event_id}'
        # Ink prints detail immediately and owns the short-lived status label.
        # SessionEvents.publish is thread-safe and never admits a turn.
        delivered = target.live.event_stream.publish(target.chat_id, {
            'text': 'Guiderails', 'detail': announcement.message, 'key': key, 'id': key,
            'level': 'info', 'kind': 'ttl', 'ttl_ms': 1500,
        }, event_type='notification.show')
        if not delivered:
            logger.debug('Guiderails Ink notice has no live recipient')
