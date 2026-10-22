"""Edit only the provider request, leaving Hermes' transcript and tool loop alone."""

import json
from collections import OrderedDict

from .guidance import DISCARD_MARKER, DISCARD_RESULT, GuidanceFactory


def _discarded(content):
    if isinstance(content, list):
        texts = [block.get('text') for block in content if isinstance(block, dict)]
        return len(texts) == 1 and _discarded(texts[0])
    if not isinstance(content, str) or DISCARD_MARKER not in content:
        return False
    try:
        return json.loads(content) == {'error': DISCARD_RESULT}
    except ValueError:
        return False


def _pair_removals(entries):
    calls, removed = {}, set()
    for position, is_call, call_id, output in entries:
        if not isinstance(call_id, str) or not call_id:
            continue
        if is_call:
            calls[call_id] = position
        else:
            call = calls.pop(call_id, None)
            if _discarded(output):
                removed.add(position)
                if call is not None:
                    removed.add(call)
    return removed


def _responses(items):
    def entries():
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind in ('function_call', 'function_call_output'):
                yield index, kind == 'function_call', item.get('call_id'), item.get('output')

    removed = _pair_removals(entries())
    return [item for index, item in enumerate(items) if index not in removed] if removed else items


def _messages(messages):
    def entries():
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get('role') == 'tool':
                yield (index, None, None), False, message.get('tool_call_id'), message.get('content')
            for field in ('tool_calls', 'content'):
                blocks = message.get(field)
                if not isinstance(blocks, list):
                    continue
                for offset, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        continue
                    position = index, field, offset
                    if field == 'tool_calls' or block.get('type') == 'tool_use':
                        yield position, True, block.get('id'), None
                    elif block.get('type') == 'tool_result':
                        yield position, False, block.get('tool_use_id'), block.get('content')
                    elif isinstance(use := block.get('toolUse'), dict):
                        yield position, True, use.get('toolUseId'), None
                    elif isinstance(result := block.get('toolResult'), dict):
                        yield position, False, result.get('toolUseId'), result.get('content')

    removed = _pair_removals(entries())
    if not removed:
        return messages
    changed = {index for index, _, _ in removed}
    filtered = []
    for index, row in enumerate(messages):
        if index not in changed:
            filtered.append(row)
            continue
        if (index, None, None) in removed:
            continue
        row = dict(row)
        for field in ('content', 'tool_calls'):
            if isinstance(row.get(field), list):
                row[field] = [block for offset, block in enumerate(row[field])
                              if (index, field, offset) not in removed]
        if not row.get('tool_calls'):
            row.pop('tool_calls', None)
        if row.get('content') or row.get('tool_calls'):
            filtered.append(row)
    return filtered


def _with_guidance(request, field, guidance, api_mode):
    guidance = GuidanceFactory.model_block(guidance)
    if field == 'input':
        items = request[field]
        if 'instructions' not in request:
            for index, item in enumerate(items):
                if isinstance(item, dict) and item.get('role') == 'developer':
                    content = item.get('content')
                    if isinstance(content, list):
                        message = dict(item, content=[*content, {'type': 'input_text', 'text': guidance}])
                    elif isinstance(content, str):
                        message = dict(item, content=content + '\n\n' + guidance)
                    else:
                        break
                    request[field] = [*items[:index], message, *items[index + 1:]]
                    return
        before = request.get('instructions') or ''
        request['instructions'] = '\n\n'.join(filter(None, (before, guidance)))
    elif request.get('__bedrock_converse__'):
        request['system'] = [*(request.get('system') or []), {'text': guidance}]
    elif 'system' in request or 'anthropic' in api_mode:
        before = request.get('system')
        request['system'] = ([*before, {'type': 'text', 'text': guidance}] if isinstance(before, list)
                             else '\n\n'.join(filter(None, (before, guidance))))
    else:
        messages = request[field]
        index = next((i for i, row in enumerate(messages) if isinstance(row, dict)
                      and row.get('role') in ('system', 'developer')), None)
        if index is None:
            request[field] = [{'role': 'system', 'content': guidance}, *messages]
        else:
            row = messages[index]
            before = row.get('content')
            content = ([*before, {'type': 'text', 'text': guidance}] if isinstance(before, list)
                       else '\n\n'.join(filter(None, (before, guidance))))
            request[field] = [*messages[:index], dict(row, content=content), *messages[index + 1:]]


class ModelContinuation:
    """Called under PluginRuntime's lock; one logical request can be built again on retry."""

    def __init__(self):
        self.pending = {}
        self.delivered = OrderedDict()

    def enqueue(self, *, session_id, origin, event_id, guidance):
        """Queue guidance and report whether this is a new pending entry."""
        if session_id and not any(event_id in ids for _, ids in self.delivered.values()):
            pending = self.pending.setdefault(session_id, {})
            added = event_id not in pending
            pending[event_id] = (origin, guidance)
            return added
        return False

    def rewrite(self, request, *, session_id, api_request_id, api_mode, auxiliary=False):
        field = next((name for name in ('input', 'messages') if isinstance(request.get(name), list)), None)
        if not field:
            return None
        before = request[field]
        after = _responses(before) if field == 'input' else _messages(before)
        key = session_id, api_request_id
        guidance, _ = self.delivered.get(key, ('', ()))
        if not guidance and session_id and api_request_id and not auxiliary:
            pending = self.pending.get(session_id, {})
            ready = {event_id: text for event_id, (origin, text) in pending.items() if origin != api_request_id}
            if ready:
                guidance = '\n'.join(dict.fromkeys(ready.values()))
                for event_id in ready:
                    pending.pop(event_id)
                if not pending:
                    self.pending.pop(session_id)
                self.delivered[key] = guidance, frozenset(ready)
                if len(self.delivered) > 128:
                    self.delivered.popitem(last=False)
        if before is after and not guidance:
            return None
        rewritten = dict(request, **{field: after})
        if guidance:
            _with_guidance(rewritten, field, guidance, api_mode)
        return {'request': rewritten, 'source': 'guiderails'}
