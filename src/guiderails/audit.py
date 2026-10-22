"""Serialized, replayable JSONL audit export."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from .state import StorageError


MAX_RECORD_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def _regular_file(metadata):
    if stat.S_ISLNK(metadata.st_mode):
        raise StorageError('symlink_audit_file')
    if not stat.S_ISREG(metadata.st_mode):
        raise StorageError('nonregular_audit_file')


def _sync_directories(directory):
    for path in (directory, *directory.parents):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _open_regular(path, flags, mode):
    try:
        _regular_file(path.stat(follow_symlinks=False))
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
    descriptor = os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    try:
        _regular_file(os.fstat(descriptor))
        stream = os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def _read_record(stream):
    line = bytearray()
    while True:
        chunk = stream.readline(min(_READ_CHUNK_BYTES, MAX_RECORD_BYTES + 1 - len(line)))
        line.extend(chunk)
        if len(line) > MAX_RECORD_BYTES:
            raise StorageError('audit_record_too_large')
        if not chunk or chunk.endswith(b'\n'):
            return line


def _archive_timestamp(path):
    match = re.fullmatch(r'([0-9]{8}T[0-9]{6})-[0-9]{12,}-[0-9]{4,}\.jsonl', path.name)
    if match is None:
        raise StorageError('corrupt_archive_name')
    try:
        return datetime.strptime(match[1], '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise StorageError('corrupt_archive_name') from exc


class AuditWriter:
    def __init__(self, store, directory, *, rotate_bytes=10485760, rotate_utc_daily=True,
                 retention_days=30, max_archive_bytes=209715200, clock=None):
        for value in (rotate_bytes, retention_days, max_archive_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError('invalid_audit_limit')
        self.store = store
        self.directory = Path(directory).absolute()
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise StorageError('symlink_audit_path')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.archive = self.directory / 'archive'
        if self.archive.is_symlink():
            raise StorageError('symlink_audit_path')
        self.archive.mkdir(mode=0o700, exist_ok=True)
        self.path = self.directory / 'events.jsonl'
        self.rotate_bytes, self.rotate_utc_daily = rotate_bytes, rotate_utc_daily
        self.retention_days, self.max_archive_bytes = retention_days, max_archive_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _recover(self, expected):
        seen, active_day, partial = set(), None, None
        locations = {}
        archives = sorted(self.archive.glob('*.jsonl'))
        for path in archives:
            _archive_timestamp(path)
        for path in [*archives, self.path]:
            try:
                with _open_regular(path, os.O_RDONLY, 'rb') as stream:
                    day, offset = self._recover_file(path, stream, seen, locations, expected)
                    if path == self.path:
                        active_day, partial = day, offset
            except FileNotFoundError:
                if path != self.path:
                    raise
        return seen, locations, active_day, partial

    def _recover_file(self, path, stream, seen, locations=None, expected=None):
        good_offset, active_day = 0, None
        while True:
            line = _read_record(stream)
            if not line:
                return active_day, None
            if not line.endswith(b'\n'):
                if path != self.path:
                    raise StorageError('corrupt_archive')
                return active_day, good_offset
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or not event['event_id']:
                    raise ValueError('invalid_event_id')
                timestamps = [event['timestamp_utc']]
                if 'exported_utc' in event:
                    timestamps.append(event['exported_utc'])
                for timestamp in timestamps:
                    if not isinstance(timestamp, str):
                        raise ValueError('invalid_event_timestamp')
                    parsed = datetime.fromisoformat(timestamp)
                    if parsed.tzinfo is None:
                        raise ValueError('invalid_event_timestamp')
                    event_day = parsed.astimezone(timezone.utc).date().isoformat()
            except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
                raise StorageError('corrupt_audit_json') from exc
            if event['event_id'] in seen:
                raise StorageError('duplicate_audit_event')
            if expected is not None and event['event_id'] in expected:
                recovered = dict(event)
                if 'exported_utc' not in recovered:
                    raise StorageError('audit_event_mismatch')
                del recovered['exported_utc']
                if json.dumps(recovered, sort_keys=True) != json.dumps(expected[event['event_id']], sort_keys=True):
                    raise StorageError('audit_event_mismatch')
            seen.add(event['event_id'])
            if locations is not None:
                locations[event['event_id']] = path
            active_day = event_day
            good_offset = stream.tell()

    def _rotate(self, now, sequence):
        suffix = 0
        while True:
            name = now.strftime('%Y%m%dT%H%M%S') + f'-{sequence:012d}-{suffix:04d}.jsonl'
            destination = self.archive / name
            if not destination.exists() and not destination.is_symlink():
                break
            suffix += 1
        os.rename(self.path, destination)
        for directory in (self.directory, self.archive):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return destination.name

    def _retain(self, now, protected_ids=()):
        archives = []
        for path in sorted(self.archive.glob('*.jsonl')):
            timestamp = _archive_timestamp(path)
            metadata = path.stat(follow_symlinks=False)
            _regular_file(metadata)
            archives.append((path, timestamp, metadata.st_size))
        total = sum(size for _, _, size in archives)
        deleted = []
        for path, timestamp, size in archives:
            if (now - timestamp).total_seconds() > self.retention_days * 86400 or total > self.max_archive_bytes:
                if protected_ids:
                    archive_ids = set()
                    with _open_regular(path, os.O_RDONLY, 'rb') as stream:
                        self._recover_file(path, stream, archive_ids)
                    if not archive_ids.isdisjoint(protected_ids):
                        continue
                path.unlink()
                total -= size
                deleted.append(path.name)
        return deleted

    def flush(self):
        lock_path = self.directory / '.writer.lock'
        if any(path.is_symlink() for path in (lock_path, self.path, self.archive, self.directory)):
            raise StorageError('symlink_audit_path')
        processed, rotations = 0, []
        try:
            with _open_regular(lock_path, os.O_CREAT | os.O_RDWR, 'r+b') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                rows = self.store.pending_events()
                expected = {}
                for row in rows:
                    event = json.loads(row['payload'])
                    if not isinstance(event, dict) or event.get('event_id') != row['event_id']:
                        raise StorageError('corrupt_audit_json')
                    event.pop('excerpt', None)
                    event.pop('exported_utc', None)
                    expected[row['event_id']] = event
                seen, locations, active_day, partial = self._recover(expected)
                pending = []
                for row in rows:
                    if row['event_id'] in seen:
                        pending.append((row, None, None))
                        continue
                    event = expected[row['event_id']]
                    now = self.clock().astimezone(timezone.utc)
                    event['exported_utc'] = now.isoformat()
                    encoded = (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
                    if len(encoded) > MAX_RECORD_BYTES:
                        raise StorageError('audit_record_too_large')
                    pending.append((row, event, (now, encoded)))
                recovered_paths = {locations[row['event_id']] for row, _, output in pending if output is None}
                for path in recovered_paths:
                    with _open_regular(path, os.O_RDONLY, 'rb') as stream:
                        os.fsync(stream.fileno())
                if recovered_paths:
                    if any(path.parent == self.archive for path in recovered_paths):
                        _sync_directories(self.archive)
                    else:
                        _sync_directories(self.directory)
                if partial is not None:
                    with _open_regular(self.path, os.O_RDWR, 'r+b') as stream:
                        stream.truncate(partial)
                        stream.flush()
                        os.fsync(stream.fileno())
                for row, event, output in pending:
                    if output is not None:
                        now, encoded = output
                        try:
                            metadata = self.path.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            size = 0
                        else:
                            _regular_file(metadata)
                            size = metadata.st_size
                        rotated = False
                        if size and (size + len(encoded) > self.rotate_bytes or
                                     (self.rotate_utc_daily and active_day != now.date().isoformat())):
                            name = self._rotate(now, row['seq'])
                            rotated = True
                            if event['event_type'] != 'audit_rotated':
                                rotations.append(name)
                        with _open_regular(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 'ab') as stream:
                            stream.write(encoded)
                            stream.flush()
                            os.fsync(stream.fileno())
                        if size == 0 or rotated:
                            _sync_directories(self.directory)
                        seen.add(row['event_id'])
                        active_day = now.date().isoformat()
                protected_ids = {row['event_id'] for row, _, _ in pending}
                deleted = self._retain(self.clock().astimezone(timezone.utc), protected_ids)
                for row, _, _ in pending:
                    self.store.ack_event(row['event_id'])
                    processed += 1
                if rotations or deleted:
                    self.store.record_event('audit_rotated', {'decision': 'rotated', 'receipt_ref': json.dumps(
                        {'archives': rotations, 'deleted': deleted, 'retention_days': self.retention_days,
                         'max_archive_bytes': self.max_archive_bytes})})
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise StorageError('audit_export_failed') from exc
        return processed
