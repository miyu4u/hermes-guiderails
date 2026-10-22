import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from guiderails.audit import AuditWriter
from guiderails.state import StateStore, StorageError


@dataclass(frozen=True)
class Hit:
    rule_id: str = 'rule.one'
    start: int = 0
    end: int = 3
    line: int = 1
    column: int = 1
    field_path: str = 'content'
    instruction: str = 'Correct the text'
    preexisting: bool = False


class StateTests(unittest.TestCase):
    def test_deduplication_filters_pages_and_profile_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            with StateStore(path, profile='one') as store, StateStore(path, profile='two') as other:
                context = dict(session_id='s', turn_id='t', operation_id='o',
                               api_request_id='request-1', tool_call_id='unit-1')
                first = store.record_detection(context, [Hit(), replace(Hit(), start=4, end=7)])
                self.assertEqual(store.record_detection(context, [Hit()]), first)
                store.record_detection(context, [replace(Hit(), rule_id='rule.two')])
                store.record_detection(dict(context, api_request_id='request-2'), [Hit()])
                store.record_detection(dict(context, session_id='other', operation_id='other'), [Hit()])
                other.record_detection(context, [Hit()])
                self.assertEqual(store.stats()['detection_count'], 4)
                self.assertEqual(store.stats()['match_count'], 5)
                self.assertEqual(store.stats(session_id='s')['detection_count'], 3)
                self.assertEqual(store.stats(rule_id='rule.two')['detection_count'], 1)
                self.assertEqual(other.stats()['detection_count'], 1)
                page = store.report(session_id='s', operation_id='o', limit=2)
                last = store.report(session_id='s', operation_id='o', limit=2,
                                    after_sequence=page[-1]['sequence'])
                self.assertEqual(len(page), 2)
                self.assertEqual(len(last), 1)
                self.assertLess(page[-1]['sequence'], last[0]['sequence'])
                self.assertEqual(len(other.report()), 1)
                for bad in (-1, True, '1'):
                    with self.assertRaises(ValueError):
                        store.report(after_sequence=bad)

    def test_excerpt_ttl_metadata_and_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3', excerpts=True, mode='observe',
                            redactor=lambda text: text.replace('secret', '[redacted]')) as store:
                store.record_detection({'session_id': 's', 'unit_id': 'u'}, [Hit()], text='secret value')
                report = store.report()[0]
                self.assertEqual(report['excerpt'], '[redacted] value')
                self.assertEqual(report['mode'], 'observe')
                writer = AuditWriter(store, root / 'audit')
                writer.flush()
                exported = writer.path.read_text()
                self.assertNotIn('secret', exported)
                self.assertNotIn('excerpt', exported)
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store._event('pattern_changed', {}, unique_key='change')
                        raise RuntimeError('rollback')
                self.assertEqual(len(store.report()), 1)

    def test_excerpt_retention_default_and_configured_boundaries(self):
        for options, days in (({}, 1), ({'excerpt_retention_days': 7}, 7)):
            with self.subTest(days=days), tempfile.TemporaryDirectory() as folder:
                now = 1700000000.0
                with patch('guiderails.state.time.time', return_value=now) as clock:
                    with StateStore(Path(folder) / 'state.sqlite3', excerpts=True,
                                    redactor=lambda text: text, **options) as store:
                        store.record_detection({'session_id': 's', 'unit_id': 'u'}, [Hit()], text='sample')
                        expiry = now + days * 86400
                        self.assertEqual(store.db.execute('SELECT expires FROM excerpts').fetchone()[0], expiry)
                        clock.return_value = expiry - 0.001
                        self.assertEqual(store.report()[0]['excerpt'], 'sample')
                        self.assertEqual(store.db.execute('SELECT count(*) FROM excerpts').fetchone()[0], 1)
                        clock.return_value = expiry
                        self.assertNotIn('excerpt', store.report()[0])
                        self.assertEqual(store.db.execute('SELECT count(*) FROM excerpts').fetchone()[0], 0)

    def test_append_before_ack_and_partial_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                event_id = store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                store.db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE OF exported ON events "
                                 "BEGIN SELECT RAISE(FAIL,'fixture'); END")
                with self.assertRaises(StorageError):
                    writer.flush()
                self.assertEqual(store.outbox_count(), 1)
                store.db.execute('DROP TRIGGER fail_ack')
                with writer.path.open('ab') as stream:
                    stream.write(b'{"partial":')
                self.assertEqual(writer.flush(), 1)
                self.assertEqual([json.loads(line)['event_id'] for line in writer.path.read_bytes().splitlines()],
                                 [event_id])
                self.assertEqual(store.outbox_count(), 0)

    def test_recovered_records_require_file_and_directory_sync_before_ack(self):
        for rotated in (False, True):
            for failure in ('file', 'directory', 'parent'):
                with self.subTest(rotated=rotated, failure=failure), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    with StateStore(root / 'state.sqlite3') as store:
                        event_id = store.record_event('allowed', {})
                        writer = AuditWriter(store, root / 'audit')
                        with patch('guiderails.audit.os.fsync', side_effect=OSError('first sync')):
                            with self.assertRaisesRegex(StorageError, '^audit_export_failed$'):
                                writer.flush()
                        self.assertEqual(store.outbox_count(), 1)
                        path = writer.path
                        if rotated:
                            path = writer.archive / '20260101T000000-000000000001-0000.jsonl'
                            writer.path.rename(path)
                        original = path.read_bytes()
                        target = {'file': path, 'directory': path.parent, 'parent': writer.directory.parent}[failure]
                        target_stat = target.stat()
                        real_fsync = os.fsync
                        calls = []

                        def fail_target(fd):
                            current = os.fstat(fd)
                            if (current.st_dev, current.st_ino) == (target_stat.st_dev, target_stat.st_ino):
                                calls.append(fd)
                                raise OSError('recovery sync')
                            real_fsync(fd)

                        for _ in range(2):
                            with patch('guiderails.audit.os.fsync', side_effect=fail_target):
                                with self.assertRaisesRegex(StorageError, '^audit_export_failed$'):
                                    writer.flush()
                            self.assertEqual(store.outbox_count(), 1)
                            self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(writer.flush(), 1)
                        self.assertEqual(store.outbox_count(), 0)
                        self.assertEqual([json.loads(line)['event_id'] for line in path.read_bytes().splitlines()],
                                         [event_id])
                        self.assertFalse(rotated and writer.path.exists())

    def test_replay_requires_complete_matching_pending_content(self):
        for rotated in (False, True):
            for mismatch in ('incomplete', 'changed', 'extra', 'missing_exported', 'type'):
                with self.subTest(rotated=rotated, mismatch=mismatch), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    with StateStore(root / 'state.sqlite3') as store:
                        event_id = store.record_event('allowed', {'ruleset_digest': 'original'})
                        writer = AuditWriter(store, root / 'audit',
                                             clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
                        row = store.pending_events()[0]
                        event = json.loads(row['payload'])
                        event['exported_utc'] = datetime.now(timezone.utc).isoformat()
                        valid = (json.dumps(event) + '\n').encode()
                        damaged = dict(event)
                        if mismatch == 'incomplete':
                            damaged = {'event_id': event_id, 'timestamp_utc': event['timestamp_utc']}
                        elif mismatch == 'changed':
                            damaged['ruleset_digest'] = 'different'
                        elif mismatch == 'extra':
                            damaged['unexpected'] = 'value'
                        elif mismatch == 'missing_exported':
                            del damaged['exported_utc']
                        else:
                            damaged['event_version'] = True
                        original = (json.dumps(damaged) + '\n').encode()
                        path = (writer.archive / '20260101T000000-000000000001-0000.jsonl'
                                if rotated else writer.path)
                        path.write_bytes(original)
                        if rotated:
                            writer.path.write_bytes(b'{"partial":')
                        with self.assertRaisesRegex(StorageError, '^audit_event_mismatch$'):
                            writer.flush()
                        self.assertEqual(path.read_bytes(), original)
                        if rotated:
                            self.assertEqual(writer.path.read_bytes(), b'{"partial":')
                        self.assertEqual(store.outbox_count(), 1)
                        path.write_bytes(valid)
                        self.assertEqual(writer.flush(), 1)
                        self.assertEqual(writer.flush(), 0)
                        self.assertEqual(path.read_bytes(), valid)
                        self.assertEqual(store.outbox_count(), 0)

    def test_replay_rejects_mismatched_pending_payload_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                event_id = store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                event = json.loads(store.pending_events()[0]['payload'])
                event['exported_utc'] = datetime.now(timezone.utc).isoformat()
                original = (json.dumps(event) + '\n').encode()
                writer.path.write_bytes(original)
                event['event_id'] = 'different'
                store.db.execute('UPDATE events SET payload=? WHERE event_id=?', (json.dumps(event), event_id))
                with self.assertRaisesRegex(StorageError, '^corrupt_audit_json$'):
                    writer.flush()
                self.assertEqual(writer.path.read_bytes(), original)
                self.assertEqual(store.outbox_count(), 1)

    def test_replay_reads_long_lines_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                event_id = store.record_event('allowed', {'receipt_ref': 'x' * 200000})
                writer = AuditWriter(store, root / 'audit')
                writer.flush()
                original = writer.path.read_bytes()
                store.db.execute('UPDATE events SET exported=0 WHERE event_id=?', (event_id,))
                real_fdopen = os.fdopen
                sizes = []

                class CheckedStream:
                    def __init__(self, stream):
                        self.stream = stream

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return self.stream.__exit__(*args)

                    def __getattr__(self, name):
                        return getattr(self.stream, name)

                    def readline(self, size=-1):
                        sizes.append(size)
                        if not 0 < size <= 65536:
                            raise AssertionError('unbounded read')
                        return self.stream.readline(size)

                with patch('guiderails.audit.os.fdopen',
                           side_effect=lambda *args, **kwargs: CheckedStream(real_fdopen(*args, **kwargs))):
                    self.assertEqual(writer.flush(), 1)
                self.assertGreater(len(sizes), 2)
                self.assertEqual(writer.path.read_bytes(), original)

    def test_invalid_archive_and_oversized_pending_preserve_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                store.record_event('allowed', {'receipt_ref': 'metadata' * 1000})
                writer = AuditWriter(store, root / 'audit')
                original = b'{"partial":'
                writer.path.write_bytes(original)
                bad = writer.archive / 'invalid.jsonl'
                bad.write_bytes(b'')
                with self.assertRaisesRegex(StorageError, '^corrupt_archive_name$'):
                    writer.flush()
                self.assertEqual(writer.path.read_bytes(), original)
                self.assertEqual(store.outbox_count(), 1)
                bad.unlink()
                with patch('guiderails.audit.MAX_RECORD_BYTES', 1024):
                    with self.assertRaisesRegex(StorageError, '^audit_record_too_large$'):
                        writer.flush()
                self.assertEqual(writer.path.read_bytes(), original)
                self.assertEqual(store.outbox_count(), 1)
                self.assertEqual(writer.flush(), 1)
                self.assertEqual(len(writer.path.read_bytes().splitlines()), 1)

    def test_corrupt_complete_oversized_and_nonregular_files_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                for payload, error in ((b'{"bad":}\n', 'corrupt_audit_json'),
                                       (b'x' * 1025, 'audit_record_too_large')):
                    writer.path.write_bytes(payload)
                    with patch('guiderails.audit.MAX_RECORD_BYTES', 1024):
                        with self.assertRaisesRegex(StorageError, '^' + error + '$'):
                            writer.flush()
                    self.assertEqual(writer.path.read_bytes(), payload)
                    self.assertEqual(store.outbox_count(), 1)
                writer.path.unlink()
                if hasattr(os, 'mkfifo'):
                    os.mkfifo(writer.path)
                    with self.assertRaises(StorageError):
                        writer.flush()
                    self.assertTrue(writer.path.is_fifo())
                    self.assertEqual(store.outbox_count(), 1)
                    writer.path.unlink()
                self.assertEqual(writer.flush(), 1)

    def test_archive_corruption_and_oversize_do_not_change_active_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                writer.path.write_bytes(b'{"partial":')
                archive = writer.archive / '20260101T000000-000000000001-0000.jsonl'
                for content, error in ((b'{"bad":}\n', 'corrupt_audit_json'),
                                       (b'x' * 1025, 'audit_record_too_large'),
                                       (b'{"partial":', 'corrupt_archive')):
                    archive.write_bytes(content)
                    with patch('guiderails.audit.MAX_RECORD_BYTES', 1024):
                        with self.assertRaisesRegex(StorageError, '^' + error + '$'):
                            writer.flush()
                    self.assertEqual(archive.read_bytes(), content)
                    self.assertEqual(writer.path.read_bytes(), b'{"partial":')
                    self.assertEqual(store.outbox_count(), 1)
                archive.unlink()
                self.assertEqual(writer.flush(), 1)

    def test_invalid_key_and_swap_reject_without_losing_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                key = root / 'state.hmac-key'
                key.write_bytes(b'x' * 33)
                key.chmod(0o600)
                with self.assertRaisesRegex(StorageError, '^invalid_hmac_key$'):
                    store.record_detection({'unit_id': 'u'}, [Hit()], text='private')
                self.assertEqual(store.outbox_count(), 0)
                key.unlink()
                store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                writer.path.write_bytes(b'')
                outside = root / 'outside'
                outside.write_bytes(b'private')
                real_open = os.open
                swapped = False

                def open_swapped(path, flags, *args, **kwargs):
                    nonlocal swapped
                    if Path(path) == writer.path and not swapped:
                        swapped = True
                        writer.path.unlink()
                        writer.path.symlink_to(outside)
                    return real_open(path, flags, *args, **kwargs)

                with patch('guiderails.audit.os.open', side_effect=open_swapped):
                    with self.assertRaises(StorageError):
                        writer.flush()
                self.assertEqual(outside.read_bytes(), b'private')
                self.assertEqual(store.outbox_count(), 1)
                writer.path.unlink()
                self.assertEqual(writer.flush(), 1)

    def test_rotation_and_duplicate_id_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                writer = AuditWriter(store, root / 'audit', rotate_bytes=1, clock=lambda: now)
                store.record_event('allowed', {})
                store.record_event('allowed', {})
                self.assertEqual(writer.flush(), 2)
                archives = list(writer.archive.glob('*.jsonl'))
                self.assertEqual(len(archives), 1)
                original = writer.path.read_bytes()
                writer.path.write_bytes(archives[0].read_bytes() + original)
                pending = store.outbox_count()
                with self.assertRaisesRegex(StorageError, '^duplicate_audit_event$'):
                    writer.flush()
                self.assertEqual(store.outbox_count(), pending)
                self.assertEqual(writer.path.read_bytes(), archives[0].read_bytes() + original)

    def test_failed_open_closes_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'state.sqlite3'
            path.write_bytes(b'not a database')
            connection = sqlite3.connect(path)
            with patch('guiderails.state.sqlite3.connect', return_value=connection):
                with self.assertRaises(StorageError):
                    StateStore(path)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_retention_failure_keeps_pending_and_replays_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                event_id = store.record_event('allowed', {})
                writer = AuditWriter(store, root / 'audit')
                with patch.object(writer, '_retain', side_effect=OSError('archive fixture')):
                    with self.assertRaises(StorageError):
                        writer.flush()
                self.assertEqual(store.outbox_count(), 1)
                self.assertEqual(writer.flush(), 1)
                self.assertEqual(store.outbox_count(), 0)
                self.assertEqual([json.loads(line)['event_id'] for line in writer.path.read_bytes().splitlines()],
                                 [event_id])

    def test_retention_does_not_delete_rotated_unacknowledged_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3') as store:
                ids = [store.record_event('allowed', {'attempt_id': str(i)}) for i in range(2)]
                writer = AuditWriter(store, root / 'audit', rotate_bytes=1, max_archive_bytes=1)
                store.db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE OF exported ON events "
                                 "BEGIN SELECT RAISE(FAIL,'fixture'); END")
                with self.assertRaises(StorageError):
                    writer.flush()
                self.assertEqual(store.outbox_count(), 2)
                self.assertTrue(list(writer.archive.glob('*.jsonl')))
                store.db.execute('DROP TRIGGER fail_ack')
                self.assertEqual(writer.flush(), 2)
                lines = []
                for path in [*writer.archive.glob('*.jsonl'), writer.path]:
                    lines.extend(json.loads(line)['event_id'] for line in path.read_bytes().splitlines())
                self.assertCountEqual(lines, ids)

    def test_multiple_processes_export_each_id_once(self):
        source = """
import sys
from pathlib import Path
from guiderails.state import StateStore
from guiderails.audit import AuditWriter
root, label = Path(sys.argv[1]), sys.argv[2]
with StateStore(root / 'state.sqlite3') as store:
    for index in range(10):
        store.record_event('allowed', {'attempt_id': f'{label}-{index}'})
    AuditWriter(store, root / 'audit').flush()
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with StateStore(root / 'state.sqlite3'):
                pass
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src'))
            processes = [subprocess.Popen([sys.executable, '-B', '-c', source, str(root), label],
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for label in ('one', 'two')]
            for process in processes:
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
            with StateStore(root / 'state.sqlite3') as store:
                AuditWriter(store, root / 'audit').flush()
                ids = [json.loads(line)['event_id'] for line in (root / 'audit/events.jsonl').read_bytes().splitlines()]
                self.assertEqual(len(ids), 20)
                self.assertEqual(len(set(ids)), 20)
                self.assertEqual(store.outbox_count(), 0)


if __name__ == '__main__':
    unittest.main()
