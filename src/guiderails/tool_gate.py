"""Translate a single inspected call to a Hermes hook decision."""

import logging
import time

from .adapters import ToolInspector, check_argument_budget, validate_read_arguments
from .engine import InspectionError, Limits, RESOURCE_LIMITS
from .state import StorageError, utc_now

READ_TOOLS = ('read_file', 'search_files')
CALL_REJECTIONS = frozenset({
    'invalid_tool_payload', 'invalid_or_unreadable_tool_payload', 'invalid_nested_calls',
    'invalid_patch', 'invalid_add_patch', 'empty_patch', 'duplicate_patch_path', 'parallel_file_conflict',
    'unsupported_empty_edit', 'unsupported_patch_mode', 'unsupported_patch_header', 'unsupported_patch_line',
    'opaque_effect', 'unsupported_tool', 'unverified_external_revision', 'untrusted_path',
    'ambiguous_file_format', 'ambiguous_insertion_patch', 'ambiguous_or_fuzzy_edit', 'ambiguous_or_fuzzy_patch',
})
logger = logging.getLogger(__name__)


def read_only_call(tool_name, arguments):
    if tool_name not in READ_TOOLS:
        return False
    limits = Limits()
    deadline = time.monotonic() + limits.max_inspection_ms / 1000
    try:
        validate_read_arguments(tool_name, arguments)
        check_argument_budget(arguments, limits.max_candidate_bytes, deadline)
    except RecursionError as exc:
        raise InspectionError('tool_nesting_limit') from exc
    except (TypeError, ValueError) as exc:
        raise InspectionError('invalid_tool_payload') from exc
    return True


class ToolGate:
    def __init__(self, manager, *, file_context, on_bypass, on_decision, continuation):
        self.manager = manager
        self.guidance = manager.config.guidance
        self.on_bypass = on_bypass
        self.on_decision = on_decision
        self.continuation = continuation
        self.inspector = ToolInspector(manager.engine, effect_policy=manager.config.effect_policy,
                                       path_context=file_context)
        self.snapshot = manager.engine.snapshot
        for name in READ_TOOLS:
            self.inspector.register_tool(name, effect='read')
        self.capture_excerpt = manager.store.excerpts
        self.max_rejections = manager.config.max_rejections

    def inspect(self, tool_name, arguments):
        excerpt_source = []

        def capture_first_match(text):
            if not excerpt_source:
                excerpt_source.append(text)

        result = self.inspector.inspect_call(tool_name, arguments, first=True,
                                             on_match=capture_first_match if self.capture_excerpt else None,
                                             _snapshot=self.snapshot)
        return result, excerpt_source[0] if excerpt_source else None

    def decide(self, tool_name, metadata, result, excerpt):
        if result.error_code:
            if result.error_code in CALL_REJECTIONS:
                return self.guidance.inspection_rejection(result.error_code)
            if result.error_code not in RESOURCE_LIMITS:
                logger.warning('Guiderails inspection unavailable: %s', result.error_code)
                return None
            if not result.hits:
                self.on_bypass(result.error_code, metadata)
                return None
        if result.hits:
            context = {name: str(metadata.get(name) or '') for name in
                       ('session_id', 'turn_id', 'operation_id', 'attempt_id', 'generation_id')}
            context['operation_id'] = context['operation_id'] or str(metadata.get('task_id') or '')
            context['turn_id'] = context['turn_id'] or context['operation_id']
            context['attempt_id'] = context['attempt_id'] or str(metadata.get('api_request_id') or '')
            call_id = str(metadata.get('tool_call_id') or '')
            context.update(tool_call_id=call_id, unit_id=call_id or result.digest,
                           tool_name=tool_name, ruleset_digest=result.ruleset_digest,
                           surface='tool.arguments', timestamp_utc=utc_now())
            try:
                event_id, decision, new_decision = self.manager.store.record_detection(
                    context, result.hits, text=excerpt, max_rejections=self.max_rejections)
            except StorageError as exc:
                if str(exc) != 'outbox_limit':
                    raise
                # The failed transaction rolled back. Export the backlog before reserving a new event.
                self.manager.audit.flush()
                event_id, decision, new_decision = self.manager.store.record_detection(
                    context, result.hits, text=excerpt, max_rejections=self.max_rejections)
            try:
                self.manager.audit.flush()
            except Exception:
                logger.warning('Guiderails audit export pending')
            if decision not in ('blocked', 'warned_allowed'):
                return None
            hit = next(hit for hit in result.hits if not hit.preexisting)
            instruction = hit.instruction
            if decision == 'warned_allowed':
                guidance = self.guidance.warning(hit.rule_id, hit.description, instruction)
            else:
                guidance = self.guidance.correction(hit.rule_id, hit.description, instruction)
            guidance_available = False
            try:
                queued = self.continuation.enqueue(session_id=context['session_id'], origin=context['attempt_id'],
                                                   event_id=event_id, guidance=guidance)
                guidance_available = bool(context['session_id'])
                if new_decision and (decision == 'warned_allowed' or queued):
                    self.on_decision(context['session_id'], event_id, tool_name,
                                     'WARNED' if decision == 'warned_allowed' else 'BLOCKED')
            except Exception:
                logger.warning('Guiderails could not queue a decision notice')
            if decision == 'warned_allowed':
                return None
            return self.guidance.pattern_rejection(
                '' if guidance_available else instruction,
                discard_result=not hit.keep and bool(call_id) and guidance_available,
            )
        return None
