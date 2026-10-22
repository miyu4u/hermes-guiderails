"""Profile policy snapshots and the trusted rule approval boundary."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .audit import AuditWriter
from .config import RuntimeConfig
from .engine import InspectionError, RuleEngine
from .fileio import FileIOError, FileTooLargeError, read_bytes, read_text
from .rules import PatternError, parse
from .state import StateStore, StorageError


MAX_DOCUMENT_BYTES = 1024 * 1024
_ID = re.compile(r'[0-9a-f]{32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
logger = logging.getLogger(__name__)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content):
    if isinstance(content, str):
        content = content.encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.guiderails-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _digest(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _document(rules):
    return _json({'schema_version': 1, 'rules': rules})


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate_json_key')
    return result


@dataclass(frozen=True)
class Draft:
    draft_id: str
    digest: str
    document: str
    warnings: tuple[str, ...] = ()


class PatternManager:
    def __init__(self, home, *, authorizer, provider=None, profile='default', limits=None, config=None, redactor=None):
        if not callable(authorizer):
            raise ValueError('management_authorizer_required')
        self.authorizer, self.provider = authorizer, provider
        self.root = Path(home).expanduser().absolute() / 'guiderails'
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise PatternError('symlink_management_root')
        self.patterns = self.root / 'patterns'
        self.drafts = self.root / 'drafts'
        self._managed = self.patterns / 'managed.yaml'
        self._journal = self.root / '.pattern-change'
        for directory in (self.root, self.patterns, self.drafts):
            if directory.is_symlink():
                raise PatternError('symlink_management_directory')
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config is None:
            config = RuntimeConfig.from_profile(self.root)
        self.config = config
        self.limits = limits if limits is not None else config.limits
        self._engine = None
        self.last_error = ''

        logging = config.as_dict()['logging']
        self.store = StateStore(
            self.root / 'state.sqlite3', profile=profile, mode=config.mode,
            max_outbox=logging['max_outbox'], excerpts=logging['excerpts'],
            excerpt_chars=logging['excerpt_chars'], redactor=redactor,
            excerpt_retention_days=logging['excerpt_retention_days'],
        )
        try:
            with self.store.transaction():
                self.store.db.execute('''CREATE TABLE IF NOT EXISTS rule_revisions (
                    profile TEXT NOT NULL, rule_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    content TEXT NOT NULL, PRIMARY KEY(profile, rule_id))''')
            self.audit = AuditWriter(
                self.store, self.root / 'audit', rotate_bytes=logging['rotate_bytes'],
                rotate_utc_daily=logging['rotate_utc_daily'], retention_days=logging['retention_days'],
                max_archive_bytes=logging['max_archive_bytes'],
            )
            with self._lock():
                self._reconcile()
                try:
                    self._load()
                except (PatternError, InspectionError) as exc:
                    self.last_error = str(exc).partition(':')[0]
        except BaseException:
            self.store.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.store.close()

    @property
    def engine(self):
        if self._engine is None:
            raise InspectionError('no_valid_ruleset')
        return self._engine

    def _authorize(self, principal, action):
        if self.authorizer(principal, action) is not True:
            raise PermissionError('management_permission_denied')

    @contextmanager
    def _lock(self):
        path = self.root / '.management.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PatternError('invalid_management_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _sources(self, *, skip_managed=False, size=0):
        rules = []
        for path in sorted(self.patterns.glob('*.yaml')):
            if skip_managed and path == self._managed:
                continue
            try:
                content = read_bytes(path, MAX_DOCUMENT_BYTES - size)
                text = content.decode('utf-8')
            except FileTooLargeError as exc:
                raise PatternError('pattern_sources_too_large') from exc
            except (FileIOError, OSError, UnicodeError) as exc:
                raise PatternError('invalid_pattern_source') from exc
            size += len(content)
            rules.extend(parse(text, allow_empty=True)['rules'])
            if len(rules) > self.limits.max_rules:
                raise PatternError('rules_limit')
        return rules

    def _accept(self, candidate):
        if self._engine is None:
            self._engine = candidate
        else:
            self._engine._snapshot = candidate.snapshot
        self.last_error = ''

    @staticmethod
    def _check_revisions(previous, incoming):
        by_id = {rule['id']: rule for rule in previous}
        for rule in incoming:
            old = by_id.get(rule['id'])
            if old is not None and old != rule and rule['revision'] <= old['revision']:
                raise PatternError('nonincreasing_revision')

    def _record_revisions(self, rules, *, persist=True):
        for rule in rules:
            content = _json(rule)
            row = self.store.db.execute(
                'SELECT revision, content FROM rule_revisions WHERE profile=? AND rule_id=?',
                (self.store.profile, rule['id']),
            ).fetchone()
            if row is not None:
                if content == row['content']:
                    continue
                if rule['revision'] <= row['revision']:
                    raise PatternError('nonincreasing_revision')
            if not persist:
                continue
            self.store.db.execute(
                '''INSERT INTO rule_revisions(profile, rule_id, revision, content) VALUES(?,?,?,?)
                   ON CONFLICT(profile, rule_id) DO UPDATE SET
                     revision=excluded.revision, content=excluded.content''',
                (self.store.profile, rule['id'], rule['revision'], content),
            )

    def _load(self):
        candidate = RuleEngine.from_yaml(_document(self._sources()), limits=self.limits)
        with self.store.transaction():
            self._record_revisions(candidate.rules)
        self._accept(candidate)

    def reload(self, *, principal):
        self._authorize(principal, 'reload')
        with self._lock():
            self._reconcile()
            try:
                rules = self._sources()
                candidate = RuleEngine.from_yaml(_document(rules), limits=self.limits)
                with self.store.transaction():
                    self._record_revisions(candidate.rules)
                    self.store._event('rules_reloaded', {'ruleset_digest': candidate.digest})
            except (PatternError, InspectionError) as exc:
                self.last_error = str(exc).partition(':')[0]
                self.store.record_event('inspection_error', {'error_code': 'reload_failed'})
                raise
            self._accept(candidate)
            return candidate.digest

    def propose(self, request, *, principal, provider=None):
        proposal = self.generate_proposal(request, principal=principal, provider=provider)
        return self.save_proposal(proposal, principal=principal)

    def generate_proposal(self, request, *, principal, provider=None):
        """Call the provider without touching mutable profile storage."""
        self._authorize(principal, 'propose')
        try:
            valid = isinstance(request, str) and request.strip() and len(request.encode('utf-8')) <= 16384
        except UnicodeError:
            valid = False
        if not valid:
            raise PatternError('invalid_pattern_request')
        callback = self.provider if provider is None else provider
        if not callable(callback):
            raise PatternError('pattern_provider_required')
        schema_path = Path(__file__).with_name('schemas') / 'pattern-set.v1.schema.json'
        schema = json.loads(read_text(schema_path, MAX_DOCUMENT_BYTES))
        contract = {
            'schema': schema, 'output': 'pattern-set YAML or JSON object', 'enabled': False,
            'requirements': ['match or regex', 'positive and negative corpus', 'bounded instruction', 'data only'],
        }
        try:
            return callback(request, contract)
        except Exception as exc:
            raise PatternError('pattern_provider_failed') from exc

    def save_proposal(self, proposal, *, principal):
        """Recheck authority and persist a validated, inactive draft."""
        self._authorize(principal, 'propose')
        try:
            data = parse(_json(proposal) if isinstance(proposal, dict) else proposal)
        except PatternError:
            raise
        except (ValueError, TypeError, RecursionError) as exc:
            raise PatternError('invalid_pattern_proposal') from exc
        for rule in data['rules']:
            rule['enabled'] = False
        validated = RuleEngine.from_yaml(_document(data['rules']), limits=self.limits)
        warnings = tuple(
            'no_native_tool_scope:' + rule['id'] for rule in data['rules']
            if not {'tool.arguments', 'artifact.text'}.intersection(rule['scopes'])
        )
        draft = Draft(uuid.uuid4().hex, validated.digest, validated.snapshot.document, warnings)
        content = _json(asdict(draft)).encode('utf-8')
        if len(content) > MAX_DOCUMENT_BYTES:
            raise PatternError('draft_too_large')
        atomic_write(self.drafts / (draft.draft_id + '.json'), content)
        return draft

    def _draft(self, draft_id):
        if not isinstance(draft_id, str) or not _ID.fullmatch(draft_id):
            raise PatternError('invalid_draft_id')
        try:
            text = read_text(self.drafts / (draft_id + '.json'), MAX_DOCUMENT_BYTES)
        except (OSError, FileIOError) as exc:
            raise PatternError('invalid_draft_path') from exc
        except UnicodeError as exc:
            raise PatternError('invalid_draft_data') from exc
        try:
            data = json.loads(text, object_pairs_hook=_unique_fields)
            if (not isinstance(data, dict) or data.keys() != {'draft_id', 'digest', 'document', 'warnings'}
                    or any(not isinstance(data[key], str) for key in ('draft_id', 'digest', 'document'))
                    or not _DIGEST.fullmatch(data['digest'])
                    or not isinstance(data['warnings'], list)
                    or any(not isinstance(warning, str) for warning in data['warnings'])):
                raise ValueError('invalid_draft_shape')
            digest = _digest(data['document'].encode('utf-8'))
        except (ValueError, RecursionError) as exc:
            raise PatternError('invalid_draft_data') from exc
        if data['draft_id'] != draft_id or not hmac.compare_digest(digest, data['digest']):
            raise PatternError('draft_digest_mismatch')
        return Draft(draft_id, digest, data['document'], tuple(data['warnings']))

    def approve(self, draft_id, *, expected_digest, principal):
        self._authorize(principal, 'apply')
        draft = self._draft(draft_id)
        if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest) or not hmac.compare_digest(draft.digest, expected_digest):
            raise PatternError('approval_digest_mismatch')
        incoming = parse(draft.document)['rules']
        for rule in incoming:
            if rule['enabled']:
                raise PatternError('invalid_draft_data')
            rule['enabled'] = True
        with self._lock():
            self._reconcile()
            return self._apply(incoming, self._read_managed(), origin=draft_id)

    def _read_managed(self):
        try:
            return read_bytes(self._managed, MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            return None
        except (FileIOError, OSError) as exc:
            raise PatternError('invalid_managed_patterns') from exc

    @staticmethod
    def _managed_rules(content):
        if content is None:
            return []
        try:
            return parse(content.decode('utf-8'), allow_empty=True)['rules']
        except UnicodeError as exc:
            raise PatternError('invalid_managed_patterns') from exc

    def _clear_journal(self):
        try:
            self._journal.unlink()
            _sync_directory(self.root)
        except OSError as exc:
            raise StorageError('pattern_change_journal_cleanup_failed') from exc

    def _reconcile(self):
        # The durable pointer avoids searching audit history on ordinary startups.
        # The prepared event carries the exact old and new managed file hashes.
        try:
            operation = read_text(self._journal, 32)
        except FileNotFoundError:
            return
        except (FileIOError, OSError, UnicodeError) as exc:
            raise StorageError('invalid_pattern_change_journal') from exc
        if not _ID.fullmatch(operation):
            raise StorageError('invalid_pattern_change_journal')
        events = self.store.report(operation_id=operation)
        if not events:
            # A process can exit after marking the operation but before preparing it.
            self._clear_journal()
            return
        prepared = next((event for event in events if event['event_type'] == 'pattern_change_prepared'), None)
        if prepared is None:
            raise StorageError('pattern_change_effect_unknown')
        committed = any(event['event_type'] == 'pattern_changed' for event in events)
        current = _digest(self._read_managed())
        if committed:
            if current != prepared['managed_after_digest']:
                raise StorageError('pattern_change_effect_unknown')
        elif current != prepared['managed_before_digest']:
            if current != prepared['managed_after_digest']:
                raise StorageError('pattern_change_effect_unknown')
            context = {key: prepared[key] for key in ('operation_id', 'ruleset_digest', 'receipt_ref', 'decision')}
            if self.store.outbox_count() >= self.store.max_outbox:
                self.audit.flush()
            candidate = RuleEngine.from_yaml(_document(self._sources()), limits=self.limits)
            if candidate.digest != context['ruleset_digest']:
                raise StorageError('pattern_change_effect_unknown')
            self._commit_change(context, prepared['rule_ids'], candidate.rules)
        self._clear_journal()

    def _commit_change(self, context, rule_ids, rules):
        with self.store.transaction():
            self._record_revisions(rules)
            self.store._event('pattern_changed', context, rule_ids=rule_ids,
                              unique_key='apply:' + context['operation_id'])

    def apply(self, text, *, principal, origin='manual'):
        self._authorize(principal, 'apply')
        incoming = parse(text)['rules']
        with self._lock():
            self._reconcile()
            return self._apply(incoming, self._read_managed(), origin=origin)

    def _apply(self, incoming, old, *, origin, previous=None):
        if previous is None:
            previous = self._managed_rules(old)
        self._check_revisions(previous, incoming)
        managed = {rule['id']: rule for rule in previous}
        managed.update((rule['id'], rule) for rule in incoming)
        document = (_document(list(managed.values())) + '\n').encode('utf-8')
        if len(document) > MAX_DOCUMENT_BYTES:
            raise PatternError('invalid_managed_patterns')
        external = self._sources(skip_managed=True, size=len(document))
        external_ids = {rule['id'] for rule in external}
        if external_ids.intersection(managed):
            raise PatternError('manual_source_conflict')
        rules = external + list(managed.values())
        candidate = RuleEngine.from_yaml(_document(rules), limits=self.limits)
        with self.store.transaction():
            self._record_revisions(candidate.rules, persist=False)
        self._replace_managed(old, document, candidate, origin, [rule['id'] for rule in incoming])
        self._accept(candidate)
        try:
            self._clear_journal()
        except StorageError:
            # The file and SQLite revision/audit state have already committed.
            # A remaining journal is reconciled before the next policy change.
            logger.warning('Guiderails committed policy change; journal cleanup could not complete')
        return candidate.digest

    def _replace_managed(self, old, document, candidate, origin, rule_ids):
        operation = uuid.uuid4().hex
        context = {'operation_id': operation, 'ruleset_digest': candidate.digest,
                   'receipt_ref': origin, 'decision': 'approved'}
        before, after = _digest(old), _digest(document)
        atomic_write(self._journal, operation)
        try:
            with self.store.transaction():
                self.store._event(
                    'pattern_change_prepared', context, rule_ids=rule_ids, unique_key='prepare:' + operation,
                    extras={'managed_before_digest': before, 'managed_after_digest': after},
                )
            if document != old:
                atomic_write(self._managed, document)
            self._commit_change(context, rule_ids, candidate.rules)
        except BaseException:
            try:
                current = _digest(self._read_managed())
                if current != before:
                    if current != after:
                        raise StorageError('pattern_change_effect_unknown')
                    if old is None:
                        self._managed.unlink()
                        _sync_directory(self.patterns)
                    else:
                        atomic_write(self._managed, old)
                self._clear_journal()
            except (OSError, PatternError, StorageError) as exc:
                raise StorageError('pattern_change_effect_unknown') from exc
            raise

    def set_enabled(self, rule_id, enabled, *, expected_revision, principal):
        if not isinstance(enabled, bool):
            raise PatternError('invalid_enabled_flag')
        self._authorize(principal, 'enable' if enabled else 'disable')
        self._authorize(principal, 'apply')
        with self._lock():
            self._reconcile()
            old = self._read_managed()
            previous = self._managed_rules(old)
            rule = next((rule for rule in previous if rule['id'] == rule_id), None)
            if rule is None or type(expected_revision) is not int or rule['revision'] != expected_revision:
                raise PatternError('rule_revision_conflict')
            changed = dict(rule, enabled=enabled, revision=expected_revision + 1)
            return self._apply([changed], old, origin='manual', previous=previous)

    def list_patterns(self, *, principal):
        self._authorize(principal, 'read')
        return self._engine.rules if self._engine is not None else []

    def test(self, rule_id, *, principal, samples=()):
        self._authorize(principal, 'read')
        if not isinstance(rule_id, str) or not isinstance(samples, (list, tuple)) or any(
            not isinstance(sample, str) for sample in samples
        ):
            raise PatternError('invalid_management_arguments')
        selected = next((rule for rule in self.engine.rules if rule['id'] == rule_id), None)
        if selected is None:
            raise PatternError('unknown_rule_id')
        selected['enabled'] = True
        engine = RuleEngine.from_yaml(_document([selected]), limits=self.limits)
        return {
            'rule_id': rule_id, 'passed': True, 'positive_count': len(selected['tests']['positive']),
            'negative_count': len(selected['tests']['negative']),
            'samples': [bool(engine.inspect(sample, scope=selected['scopes'][0], sample=True))
                        for sample in samples],
        }

    def status(self, *, principal):
        self._authorize(principal, 'read')
        engine = self._engine
        return {
            'ready': engine is not None, 'mode': self.config.mode, 'effect_policy': self.config.effect_policy,
            'ruleset_digest': engine.digest if engine is not None else None,
            'rule_count': len(engine.rules) if engine is not None else 0,
            'error_code': self.last_error, 'outbox_count': self.store.outbox_count(),
        }

    def stats(self, *, principal, **filters):
        self._authorize(principal, 'read')
        if any(key != 'session_id' or not isinstance(value, str) or not value for key, value in filters.items()):
            raise PatternError('invalid_management_filters')
        return self.store.stats(**filters)

    def report(self, *, principal, **filters):
        self._authorize(principal, 'read')
        for key, value in filters.items():
            if key in ('session_id', 'operation_id'):
                valid = isinstance(value, str) and bool(value)
            elif key == 'limit':
                valid = type(value) is int and 1 <= value <= 10000
            elif key == 'after_sequence':
                valid = type(value) is int and 0 <= value <= 2**63 - 1
            else:
                valid = False
            if not valid:
                raise PatternError('invalid_management_filters')
        return self.store.report(**filters)
