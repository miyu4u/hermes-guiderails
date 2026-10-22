"""Transactional profile state, detection counters and audit outbox."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .fileio import FileIOError, read_key


class StorageError(RuntimeError):
    """A storage operation failed."""


COUNTERS = ('match_count', 'detection_count', 'blocked_attempt_count', 'retry_count', 'exhausted_count', 'bypass_count',
            'preexisting_hit_count', 'inspection_error_count', 'unsupported_count')
FIELDS = ('session_id', 'turn_id', 'operation_id', 'attempt_id', 'generation_id', 'unit_id', 'ruleset_digest',
          'surface', 'tool_name', 'field_path', 'decision', 'decision_rule_id', 'retries_used', 'latency_ms',
          'partial', 'context_mode',
          'display_action_requested', 'display_action_applied', 'displayed_chars', 'effect_state', 'error_code',
          'correction_of', 'receipt_ref', 'mode', 'retry_index', 'max_retries', 'remaining_retries',
          'cancellation_reason', 'max_blocks', 'session_blocks_used', 'session_bypassed')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class StateStore:
    def __init__(self, path, *, profile='default', max_outbox=10000,
                 excerpts=False, redactor=None, excerpt_chars=160, excerpt_retention_days=1, mode='strict'):
        if isinstance(max_outbox, bool) or not isinstance(max_outbox, int) or max_outbox < 1:
            raise ValueError('invalid_outbox_limit')
        if not 1 <= excerpt_chars <= 1024:
            raise ValueError('invalid_storage_limit')
        if excerpts and not callable(redactor):
            raise ValueError('opt_in_excerpts_require_redactor')
        if mode not in ('strict', 'observe') or not 1 <= excerpt_retention_days <= 30:
            raise ValueError('invalid_privacy_or_mode_setting')
        self.path = Path(path).absolute()
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise StorageError('symlink_state_path')
        self.profile, self.max_outbox = profile, max_outbox
        self.excerpts, self.redactor, self.excerpt_chars = excerpts, redactor, excerpt_chars
        self.mode, self.excerpt_retention_days = mode, excerpt_retention_days
        self._lock = threading.RLock()
        self.db = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA busy_timeout=10000')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                profile TEXT NOT NULL, unique_key TEXT, payload TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0,
                UNIQUE(profile, unique_key));
              CREATE TABLE IF NOT EXISTS counters (
                profile TEXT, session TEXT, rule TEXT, surface TEXT, day TEXT, name TEXT, value INTEGER NOT NULL,
                PRIMARY KEY(profile,session,rule,surface,day,name));
              CREATE TABLE IF NOT EXISTS markers (profile TEXT, key TEXT, PRIMARY KEY(profile,key));
              CREATE TABLE IF NOT EXISTS detections (
                profile TEXT, unit TEXT, rule TEXT, kind TEXT, event_id TEXT,
                PRIMARY KEY(profile,unit,rule,kind));
              CREATE TABLE IF NOT EXISTS rejection_decisions (
                profile TEXT NOT NULL, unit TEXT NOT NULL, session TEXT NOT NULL, rule TEXT NOT NULL,
                decision TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(profile,unit));
              CREATE UNIQUE INDEX IF NOT EXISTS rejection_decisions_event ON rejection_decisions(event_id);
              CREATE INDEX IF NOT EXISTS rejection_decisions_budget
                ON rejection_decisions(profile,session,rule) WHERE decision IN ('reserved','blocked');
              CREATE TABLE IF NOT EXISTS excerpts (
                event_id TEXT PRIMARY KEY, profile TEXT, text TEXT NOT NULL, expires REAL NOT NULL);
            ''')
        except BaseException as exc:
            if self.db is not None:
                self.db.close()
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise StorageError('state_open_failed') from exc
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield
                self.db.execute('COMMIT')
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise StorageError('state_write_failed') from exc
                raise

    def _counter(self, name, amount, context, rule=''):
        if amount:
            self.db.execute('''INSERT INTO counters VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(profile,session,rule,surface,day,name) DO UPDATE SET value=value+excluded.value''',
              (self.profile, context.get('session_id', ''), rule, context.get('surface', ''),
               context.get('timestamp_utc', utc_now())[:10], name, amount))

    def _marker(self, key):
        return bool(self.db.execute('INSERT OR IGNORE INTO markers VALUES(?,?)', (self.profile, key)).rowcount)

    def _event(self, event_type, context, *, rule_ids=(), locations=(), unique_key=None, extras=None):
        if unique_key:
            existing = self.db.execute('SELECT event_id FROM events WHERE profile=? AND unique_key=?',
                                       (self.profile, unique_key)).fetchone()
            if existing:
                return existing[0], False
        if self.db.execute('SELECT count(*) FROM events WHERE profile=? AND exported=0',
                           (self.profile,)).fetchone()[0] >= self.max_outbox:
            raise StorageError('outbox_limit')
        event_id = uuid.uuid4().hex
        payload = {key: context.get(key) for key in FIELDS}
        payload.update(event_version=1, event_id=event_id, timestamp_utc=utc_now(), profile=self.profile,
                       event_type=event_type, rule_ids=list(rule_ids), locations=list(locations))
        payload['mode'] = context.get('mode', self.mode)
        excerpt = None
        if extras:
            extras = dict(extras)
            excerpt = extras.pop('excerpt', None)
            payload.update(extras)
        self.db.execute('INSERT INTO events(event_id,profile,unique_key,payload) VALUES(?,?,?,?)',
                        (event_id, self.profile, unique_key, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        if excerpt is not None:
            self.db.execute('INSERT INTO excerpts VALUES(?,?,?,?)',
                            (event_id, self.profile, excerpt, time.time() + self.excerpt_retention_days * 86400))
        return event_id, True

    def record_event(self, event_type, context, *, unique_key=None):
        with self.transaction():
            event_id, inserted = self._event(event_type, context, unique_key=unique_key)
            if inserted and event_type in ('inspection_error', 'unsupported'):
                self._counter(event_type + '_count', 1, context)
            return event_id

    def _content_metadata(self, text):
        if text is None:
            return {}
        key_path = self.path.with_suffix('.hmac-key')
        if key_path.is_symlink():
            raise StorageError('symlink_hmac_key')
        try:
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
            key = read_key(key_path)
            if key_path.stat().st_mode & 0o077:
                raise FileIOError('invalid_hmac_key')
        except (OSError, UnicodeDecodeError, FileIOError) as exc:
            raise StorageError('invalid_hmac_key') from exc
        result = {'content_hmac': hmac.new(key, text.encode(), hashlib.sha256).hexdigest()}
        if self.excerpts:
            try:
                excerpt = self.redactor(text)
            except Exception as exc:
                raise StorageError('redactor_failed') from exc
            if not isinstance(excerpt, str):
                raise StorageError('invalid_redactor_result')
            result['excerpt'] = excerpt[:self.excerpt_chars]
        return result

    def record_detection(self, context, hits, *, partial=False, text=None, max_rejections=None):
        """Return the audit event, decision, and whether a strict decision was newly recorded."""
        hits = tuple(hits)
        attempt = context.get('attempt_id') or context.get('api_request_id')
        unit_id = context.get('unit_id') or context.get('tool_call_id') or context.get('digest')
        context = dict(context, attempt_id=attempt, unit_id=unit_id)
        session = context.get('session_id') or ''
        unit = stable_id(session, context.get('turn_id'), context.get('operation_id'), attempt, unit_id)
        ruling_hit = next((hit for hit in hits if not hit.preexisting), None)
        budgeted = max_rejections is not None and self.mode == 'strict' and ruling_hit is not None
        if budgeted and not context.get('tool_call_id'):
            # Without a call ID identical arguments don't establish identity.
            unit = uuid.uuid4().hex
        with self.transaction():
            decision = None
            if budgeted:
                previous = self.db.execute(
                    'SELECT event_id,decision FROM rejection_decisions WHERE profile=? AND unit=?',
                    (self.profile, unit)).fetchone()
                if previous:
                    decision = previous['decision']
                    return previous['event_id'], 'blocked' if decision == 'reserved' else decision, False
                used = 0
                if session and max_rejections:
                    used = self.db.execute('''
                        SELECT count(*) FROM rejection_decisions
                        WHERE profile=? AND session=? AND rule=? AND decision IN ('reserved','blocked')
                    ''', (self.profile, session, ruling_hit.rule_id)).fetchone()[0]
                decision = 'blocked' if used < max_rejections else 'warned_allowed'
                context.update(decision=decision, decision_rule_id=ruling_hit.rule_id)
            if self.excerpts:
                self._prune_excerpts()
            existing = self.db.execute('SELECT rule,kind,event_id FROM detections WHERE profile=? AND unit=?',
                                       (self.profile, unit)).fetchall()
            seen = {(row['rule'], row['kind']) for row in existing}
            hits = tuple(h for h in hits if (h.rule_id, 'preexisting' if h.preexisting else 'blocking') not in seen)
            if not hits and existing:
                return existing[0]['event_id'], decision, False
            blocking = tuple(h for h in hits if not h.preexisting)
            rule_ids = list(dict.fromkeys(h.rule_id for h in hits))
            locations = [{'rule_id': h.rule_id, 'start': h.start, 'end': h.end, 'line': h.line, 'column': h.column,
                          'field_path': h.field_path, 'instruction': h.instruction, 'preexisting': h.preexisting} for h in hits]
            event_id, _ = self._event('detected' if blocking else 'preexisting', dict(context, partial=partial),
                                      rule_ids=rule_ids, locations=locations, extras=self._content_metadata(text))
            if budgeted:
                self.db.execute('INSERT INTO rejection_decisions VALUES(?,?,?,?,?,?)',
                                (self.profile, unit, session, ruling_hit.rule_id,
                                 decision, event_id))
            for rule_id, kind in {(h.rule_id, 'preexisting' if h.preexisting else 'blocking') for h in hits}:
                self.db.execute('INSERT INTO detections VALUES(?,?,?,?,?)', (self.profile, unit, rule_id, kind, event_id))
            for rule_id in rule_ids:
                selected = [h for h in hits if h.rule_id == rule_id]
                self._counter('match_count', sum(not h.preexisting for h in selected), context, rule_id)
                self._counter('detection_count', int(any(not h.preexisting for h in selected)), context, rule_id)
                self._counter('preexisting_hit_count', sum(h.preexisting for h in selected), context, rule_id)
            attempt_key = stable_id(context.get('session_id'), context.get('turn_id'), context.get('operation_id'), attempt)
            if decision == 'blocked' or (not budgeted and self.mode == 'strict' and blocking
                                         and self._marker('blocked:' + attempt_key)):
                self._counter('blocked_attempt_count', 1, context)
            return event_id, decision, budgeted

    def stats(self, *, session_id=None, rule_id=None, surface=None, since=None, until=None):
        where, args = ['profile=?'], [self.profile]
        for column, value, operator in (('session', session_id, '='), ('rule', rule_id, '='), ('surface', surface, '='), ('day', since, '>='), ('day', until, '<=')):
            if value is not None:
                where.append(f'{column}{operator}?')
                args.append(value)
        with self._lock:
            rows = self.db.execute('SELECT name,sum(value) FROM counters WHERE ' + ' AND '.join(where) + ' GROUP BY name', args).fetchall()
        return dict.fromkeys(COUNTERS, 0) | dict(rows)

    def outbox_count(self):
        with self._lock:
            return self.db.execute('SELECT count(*) FROM events WHERE profile=? AND exported=0', (self.profile,)).fetchone()[0]

    def prune_excerpts(self):
        with self.transaction():
            return self._prune_excerpts()

    def _prune_excerpts(self):
        return self.db.execute('DELETE FROM excerpts WHERE profile=? AND expires<=?',
                               (self.profile, time.time())).rowcount

    def pending_events(self):
        with self._lock:
            return self.db.execute('SELECT seq,event_id,payload FROM events WHERE profile=? AND exported=0 ORDER BY seq LIMIT 10000', (self.profile,)).fetchall()

    def ack_event(self, event_id):
        with self.transaction():
            self.db.execute('UPDATE events SET exported=1 WHERE profile=? AND event_id=?', (self.profile, event_id))

    def report(self, *, session_id=None, operation_id=None, limit=100, after_sequence=0):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError('invalid_report_limit')
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError('invalid_report_sequence')
        if self.excerpts:
            self.prune_excerpts()
        result = []
        with self._lock:
            cursor = self.db.execute('SELECT seq,payload FROM events WHERE profile=? AND seq>? ORDER BY seq', (self.profile, after_sequence))
            for row in cursor:
                event = json.loads(row['payload'])
                if session_id is not None and event['session_id'] != session_id:
                    continue
                if operation_id is not None and event['operation_id'] != operation_id:
                    continue
                excerpt = self.db.execute('SELECT text FROM excerpts WHERE event_id=? AND profile=? AND expires>?',
                                          (event['event_id'], self.profile, time.time())).fetchone()
                if excerpt:
                    event['excerpt'] = excerpt['text']
                result.append(dict(event, sequence=row['seq']))
                if len(result) >= limit:
                    break
        return result
