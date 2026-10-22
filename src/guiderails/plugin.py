"""Native Hermes registration and profile-scoped runtime."""

import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import re
import shlex
import sqlite3
import threading
from uuid import uuid4

from .announcement import Announcement, Announcer
from .announcement.channels.ink_transcript import InkTranscriptChannel
from .announcement.channels.post_message import PostMessageChannel
from .config import RuntimeConfig
from .continuation import ModelContinuation
from .engine import InspectionError, RESOURCE_LIMITS
from .guidance import DEFAULT_GUIDANCE
from .host import local_file_context
from .manager import PatternManager
from .rules import PatternError
from .state import StorageError
from .tool_gate import ToolGate, read_only_call

logger = logging.getLogger(__name__)


TOOL_NAME = 'guiderails_patterns'
ACTION_FIELDS = {
    'status': (),
    'list': (),
    'stats': ('session_id',),
    'report': ('session_id', 'operation_id'),
    'test': ('rule_id', 'sample'),
    'propose': ('request',),
}
FILTER_FIELDS = ('session_id', 'operation_id')
TOOL_SCHEMA = {
    'name': TOOL_NAME,
    'description': '패턴 규칙과 감지 기록을 조회하거나 사용자 검토용 비활성 초안을 작성합니다.',
    'parameters': {
        'type': 'object',
        'additionalProperties': False,
        'required': ['action'],
        'properties': {
            'action': {'type': 'string', 'enum': list(ACTION_FIELDS)},
            **{field: {'type': 'string'} for fields in ACTION_FIELDS.values() for field in fields},
        },
    },
}


def _json(**fields):
    return json.dumps(fields, ensure_ascii=True)


def _error_code(exc):
    if isinstance(exc, (PatternError, InspectionError, StorageError)):
        code = str(exc).partition(':')[0]
        if re.fullmatch(r'[a-z][a-z0-9_]{0,127}', code):
            return code
    if isinstance(exc, PermissionError):
        return 'permission_denied'
    if isinstance(exc, (OSError, sqlite3.Error)):
        return 'storage_error'
    return 'invalid_request'


def _redact_excerpt(text):
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True, file_read=True, secret_file=True,
                                 redact_url_credentials=True)


class PluginRuntime:
    def __init__(self, ctx, *, home, current_home, file_context=None, channels=None):
        self.ctx = ctx
        self.home = Path(home).resolve()
        self.current_home = current_home
        self._file_context = file_context or local_file_context
        self._profile_name = ctx.profile_name
        self._authority = object()
        self._config = None
        self._manager = None
        self._closed = False
        self._lock = threading.RLock()
        self._continuation = ModelContinuation()
        self._announcer = Announcer(
            [InkTranscriptChannel(ctx, home=self.home), PostMessageChannel(ctx)] if channels is None else channels,
            is_active=lambda: not self._closed and self._scope_matches(),
        )

    @property
    def manager(self):
        with self._lock:
            self._check_scope()
            if self._manager is None:
                self._manager = PatternManager(
                    self.home, profile=self._profile_name,
                    authorizer=lambda principal, action: principal is self._authority,
                    provider=self._propose_model,
                    redactor=_redact_excerpt,
                    config=self._config,
                )
                self._config = self._manager.config
            return self._manager

    def _early_rejection_guidance(self):
        if self._config is None:
            try:
                self._config = RuntimeConfig.from_profile(self.home / 'guiderails')
            except PatternError:
                return DEFAULT_GUIDANCE
        return self._config.guidance

    def _scope_matches(self):
        return (Path(self.current_home()).resolve() == self.home
                and self.ctx.profile_name == self._profile_name)

    def _check_scope(self):
        if self._closed:
            raise PatternError('runtime_closed')
        if not self._scope_matches():
            self._authority = object()
            raise PatternError('profile_scope_mismatch')

    def pre_tool_call(self, tool_name, args, **metadata):
        try:
            with self._lock:
                self._check_scope()
                if not isinstance(tool_name, str) or not isinstance(args, dict):
                    return self._early_rejection_guidance().invalid_tool_payload()
                if tool_name == TOOL_NAME:
                    action = args.get('action')
                    if isinstance(action, str) and action in ACTION_FIELDS:
                        return None
                    return self._early_rejection_guidance().human_approval_required()
                try:
                    if read_only_call(tool_name, args):
                        return None
                except InspectionError as exc:
                    if str(exc) not in RESOURCE_LIMITS:
                        return self._early_rejection_guidance().invalid_tool_payload()
                    self._inspection_bypassed(str(exc), metadata)
                    return None
                gate = ToolGate(self.manager, file_context=self._file_context,
                                on_bypass=self._inspection_bypassed,
                                on_decision=self._announce_decision, continuation=self._continuation)
            # Inspect against the captured policy without holding the runtime
            # lock across I/O. Persist detections only while the scope is live.
            result, excerpt = gate.inspect(tool_name, args)
            with self._lock:
                self._check_scope()
                return gate.decide(tool_name, metadata, result, excerpt)
        except Exception:
            logger.warning('Guiderails tool inspection could not complete')
            return None

    def llm_request(self, request, *, session_id='', api_request_id='', api_mode='', turn_id='', **_metadata):
        try:
            with self._lock:
                if self._closed or not self._scope_matches():
                    return None
                return self._continuation.rewrite(
                    request, session_id=session_id, api_request_id=api_request_id, api_mode=api_mode,
                    auxiliary=turn_id.endswith(':side_question'))
        except Exception:
            logger.warning('Guiderails could not rewrite the model request')
            return None

    def _announce_decision(self, session_id, event_id, tool_name, decision):
        if not session_id:
            return
        subject = tool_name if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tool_name) else 'tool'
        message = f'Guiderails · {decision} · {subject} · {event_id[:8]}'
        self._announcer.submit(Announcement(event_id, session_id, message, 'tool.arguments'))

    def _inspection_bypassed(self, reason, metadata):
        logger.debug('Guiderails inspection bypassed: %s', reason)
        session_id = metadata.get('session_id')
        if isinstance(session_id, str) and session_id:
            self._announcer.submit(
                Announcement(uuid4().hex, session_id, 'Guiderails · BYPASSED · Inspection budget exceeded',
                             'tool.arguments'),
                coalesce_key=session_id,
            )

    def model_tool(self, arguments, **_metadata):
        try:
            with self._lock:
                self._check_scope()
            if (not isinstance(arguments, dict)
                    or any(k not in TOOL_SCHEMA['parameters']['properties'] or not isinstance(v, str)
                           for k, v in arguments.items())
                    or 'action' not in arguments):
                raise PatternError('invalid_management_arguments')
            action = arguments['action']
            if action not in ACTION_FIELDS:
                raise PatternError('human_approval_command_required' if action in ('approve', 'reload')
                                   else 'invalid_management_arguments')
            extras = arguments.keys() - {'action', *ACTION_FIELDS[action]}
            if extras:
                raise PatternError('invalid_management_filters' if extras & set(FILTER_FIELDS)
                                   else 'invalid_management_arguments')
            values = {
                'propose': [arguments.get('request', '')],
                'test': [arguments.get('rule_id', ''), *([arguments['sample']] if 'sample' in arguments else [])],
            }
            return self.manage(action, values.get(action, ()),
                               filters={key: arguments[key] for key in FILTER_FIELDS if key in arguments})
        except Exception as exc:
            return _json(ok=False, error_code=_error_code(exc))

    def manage(self, action, arguments=(), *, filters=None):
        try:
            with self._lock:
                self._check_scope()
                if not isinstance(arguments, (list, tuple)) or any(not isinstance(v, str) for v in arguments):
                    raise PatternError('invalid_management_arguments')
                if filters is not None and (
                    not isinstance(filters, dict)
                    or any(k not in ACTION_FIELDS.get(action, ()) or k not in FILTER_FIELDS
                           or not isinstance(v, str) or not v for k, v in filters.items())
                ):
                    raise PatternError('invalid_management_filters')
                self._validate_command(action, arguments)
                if action != 'propose':
                    manager = self.manager
                    audit_error = None
                    if action in ('status', 'report', 'approve', 'reload'):
                        try:
                            manager.audit.flush()
                        except Exception as exc:
                            audit_error = _error_code(exc)
                    result = self._manage(manager, action, arguments, filters or {})
                    if action in ('approve', 'reload'):
                        try:
                            manager.audit.flush()
                            audit_error = None
                        except Exception as exc:
                            audit_error = _error_code(exc)
                    if audit_error is not None:
                        result.update(audit_pending=True, audit_error_code=audit_error)
                    return _json(ok=True, **result)
            return _json(ok=True, **self._propose(' '.join(arguments)))
        except Exception as exc:
            return _json(ok=False, error_code=_error_code(exc))

    @staticmethod
    def _validate_command(action, arguments):
        if action == 'approve':
            if len(arguments) != 2:
                raise PatternError('approve_requires_draft_id_and_digest')
        elif action == 'test':
            if not arguments or not arguments[0]:
                raise PatternError('test_requires_rule_id')
        elif action == 'propose':
            if not ' '.join(arguments).strip():
                raise PatternError('invalid_management_arguments')
        elif action in ('status', 'list', 'stats', 'report', 'reload'):
            if arguments:
                raise PatternError('invalid_management_arguments')
        else:
            raise PatternError('unknown_management_action')

    def _manage(self, manager, action, arguments, filters):
        principal = self._authority
        if action == 'approve':
            return {'ruleset_digest': manager.approve(arguments[0], expected_digest=arguments[1], principal=principal)}
        if action == 'reload':
            return {'ruleset_digest': manager.reload(principal=principal)}
        if action == 'status':
            return {'status': manager.status(principal=principal)}
        if action == 'list':
            return {'rules': manager.list_patterns(principal=principal)}
        if action == 'test':
            return {'test': manager.test(arguments[0], principal=principal, samples=arguments[1:])}
        if action == 'stats':
            return {'stats': manager.stats(principal=principal, **filters)}
        return {'events': manager.report(principal=principal, **filters)}

    def _propose_model(self, request, contract):
        try:
            result = self.ctx.llm.complete_structured(
                instructions='사용자 요청을 검사 규칙 데이터로 표현하세요. match 또는 regex, '
                             'artifact.text 또는 tool.arguments 범위, 교정 지침, 양성·음성 예제를 작성하세요. '
                             '새 규칙은 revision 1, enabled false로 작성하세요.',
                input=[{'type': 'text', 'text': request}], json_schema=contract['schema'],
                schema_name='guiderails_pattern_set', max_tokens=4096, timeout=90,
                purpose='guiderails.pattern_proposal',
            )
            return result.parsed if isinstance(result.parsed, dict) else result.text
        except Exception as exc:
            raise PatternError('pattern_provider_failed') from exc

    def _propose(self, request):
        with self._lock:
            manager, principal = self.manager, self._authority
        try:
            proposal = manager.generate_proposal(request, principal=principal)
        except Exception:
            with self._lock:
                self._check_proposal(manager, principal)
            raise
        with self._lock:
            self._check_proposal(manager, principal)
            draft = manager.save_proposal(proposal, principal=principal)
            return {'draft': asdict(draft), 'approval_required': True,
                    'approve_command': f'/guiderails approve {draft.draft_id} {draft.digest}'}

    def _check_proposal(self, manager, principal):
        if self._closed or self._manager is not manager or self._authority is not principal:
            raise PatternError('proposal_invalidated')
        try:
            self._check_scope()
        except Exception as exc:
            raise PatternError('proposal_invalidated') from exc

    async def command(self, raw_args=''):
        try:
            args = shlex.split(raw_args)
        except ValueError:
            return _json(ok=False, error_code='invalid_command_syntax')
        return await asyncio.to_thread(self.manage, args[0] if args else 'status', args[1:])

    def close(self):
        with self._lock:
            self._closed = True
            self._authority = object()
            self._continuation = None
            manager, self._manager = self._manager, None
            if manager is not None:
                manager.close()


def register(ctx, *, home=None, current_home=None, file_context=None, channels=None):
    if current_home is None:
        from hermes_constants import get_hermes_home
        current_home = get_hermes_home
    runtime = PluginRuntime(ctx, home=current_home() if home is None else home,
                            current_home=current_home, file_context=file_context, channels=channels)
    ctx.register_hook('pre_tool_call', runtime.pre_tool_call)
    ctx.register_middleware('llm_request', runtime.llm_request)
    ctx.register_command('guiderails', runtime.command, description='패턴 초안·승인·검사·감지 기록',
                         args_hint='<status|list|test|stats|report|propose|approve|reload> ...')
    ctx.register_tool(TOOL_NAME, toolset='guiderails', schema=TOOL_SCHEMA, handler=runtime.model_tool)
    ctx.on_unload(runtime.close)
    return runtime
