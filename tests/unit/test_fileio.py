import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiderails import fileio


class BoundedFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "input"

    def test_limits_include_empty_and_exact_boundary(self):
        self.path.write_bytes(b"")
        self.assertEqual(fileio.read_bytes(self.path, 0), b"")
        self.path.write_bytes(b"abc")
        self.assertEqual(fileio.read_bytes(self.path, 3), b"abc")
        with self.assertRaises(fileio.FileTooLargeError):
            fileio.read_bytes(self.path, 2)
        for limit in (-1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                fileio.read_bytes(self.path, limit)

    def test_utf8_validation_is_byte_bounded(self):
        self.path.write_bytes("한".encode())
        self.assertEqual(fileio.read_text(self.path, 3), "한")
        with self.assertRaises(fileio.FileTooLargeError):
            fileio.read_text(self.path, 2)
        self.path.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            fileio.read_text(self.path, 1)

    def test_key_exactly_32_arbitrary_bytes(self):
        for size in (0, 31, 32, 33):
            with self.subTest(size=size):
                data = b"\xff" * size
                self.path.write_bytes(data)
                if size == 32:
                    self.assertEqual(fileio.read_key(self.path), data)
                else:
                    with self.assertRaises(fileio.InvalidKeyLengthError):
                        fileio.read_key(self.path)

    def test_nonregular_and_missing_paths(self):
        with self.assertRaises(FileNotFoundError):
            fileio.read_bytes(self.path, 10)
        self.path.mkdir()
        with self.assertRaises(fileio.NonRegularFileError):
            fileio.read_bytes(self.path, 10)
        self.path.rmdir()
        os.mkfifo(self.path)
        with self.assertRaises(fileio.NonRegularFileError):
            fileio.read_bytes(self.path, 10)
        self.path.unlink()
        self.path.symlink_to(Path(self.temporary.name) / "missing")
        with self.assertRaises(fileio.NonRegularFileError):
            fileio.read_bytes(self.path, 10)

    def test_fifo_replacement_is_opened_nonblocking_and_not_read(self):
        self.path.write_bytes(b"original")
        original_open = os.open

        def replace_then_open(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK)
            self.path.unlink()
            os.mkfifo(self.path)
            return original_open(path, flags)

        with mock.patch.object(fileio.os, "open", side_effect=replace_then_open), \
             mock.patch.object(fileio.os, "read") as read:
            with self.assertRaises(fileio.NonRegularFileError):
                fileio.read_bytes(self.path, 100)
            read.assert_not_called()

    def test_symlink_replacement_is_not_followed(self):
        self.path.write_bytes(b"original")
        target = self.path.with_name("target")
        target.write_bytes(b"secret")
        original_open = os.open

        def replace_then_open(path, flags):
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.path.unlink()
            self.path.symlink_to(target)
            return original_open(path, flags)

        with mock.patch.object(fileio.os, "open", side_effect=replace_then_open), \
             mock.patch.object(fileio.os, "read") as read:
            with self.assertRaises(fileio.NonRegularFileError):
                fileio.read_bytes(self.path, 100)
            read.assert_not_called()

    def test_opened_file_size_is_checked_after_replacement(self):
        self.path.write_bytes(b"a")
        replacement = self.path.with_name("replacement")
        replacement.write_bytes(b"abcd")
        original_open = os.open

        def replace_then_open(path, flags):
            os.replace(replacement, self.path)
            return original_open(path, flags)

        with mock.patch.object(fileio.os, "open", side_effect=replace_then_open), \
             mock.patch.object(fileio.os, "read") as read:
            with self.assertRaises(fileio.FileTooLargeError):
                fileio.read_bytes(self.path, 3)
            read.assert_not_called()

    def test_growth_and_short_reads_remain_bounded(self):
        self.path.write_bytes(b"ab")
        original_read = os.read
        requested = []

        def grow_and_read(fd, count):
            requested.append(count)
            if len(requested) == 1:
                with self.path.open("ab") as stream:
                    stream.write(b"cdef")
            return original_read(fd, min(count, 1))

        with mock.patch.object(fileio.os, "read", side_effect=grow_and_read):
            with self.assertRaises(fileio.FileTooLargeError):
                fileio.read_bytes(self.path, 3)
        self.assertEqual(requested, [4, 3, 2, 1])

    def test_os_errors_propagate(self):
        self.path.write_bytes(b"abc")
        with mock.patch.object(fileio.os, "open", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(PermissionError):
                fileio.read_bytes(self.path, 3)
