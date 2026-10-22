"""Read bounded regular files without following the final path component.

``NonRegularFileError`` identifies a FIFO, symlink, or other non-regular
file; candidate inspection maps it to ``nonregular_file``. ``FileTooLargeError``
and ``InvalidKeyLengthError`` identify invalid sizes. All three derive from
``FileIOError`` so policy and management callers can map them to their own
input errors. OS errors and UTF-8 decoding errors propagate to the caller.
Only the last path component is protected against symlinks; callers own
namespace and parent-directory validation.
"""

import errno
import os
import stat


class FileIOError(Exception):
    """The input does not satisfy the bounded-file contract."""


class NonRegularFileError(FileIOError):
    """The final component is not a regular file."""


class FileTooLargeError(FileIOError):
    """The file exceeds its byte limit."""


class InvalidKeyLengthError(FileIOError):
    """An HMAC key is not exactly 32 bytes."""


def read_bytes(path, max_bytes):
    """Return at most max_bytes from a regular file, rejecting excess bytes.

    Both the path and the opened descriptor are checked. Nonblocking,
    no-follow open prevents a last-component FIFO or symlink replacement from
    blocking or being read. At most max_bytes + 1 bytes are read even when a
    regular file grows after its descriptor is inspected.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")

    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise NonRegularFileError("not a regular file")
    if before.st_size > max_bytes:
        raise FileTooLargeError("file exceeds byte limit")

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise NonRegularFileError("final component is a symlink") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise NonRegularFileError("not a regular file")
        if opened.st_size > max_bytes:
            raise FileTooLargeError("file exceeds byte limit")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise FileTooLargeError("file exceeds byte limit")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_text(path, max_bytes):
    """Read a bounded UTF-8 document; decoding errors propagate unchanged."""
    return read_bytes(path, max_bytes).decode("utf-8")


def read_key(path):
    """Read an HMAC key that is exactly 32 bytes, without text decoding."""
    try:
        key = read_bytes(path, 32)
    except FileTooLargeError as exc:
        raise InvalidKeyLengthError("HMAC key must be 32 bytes") from exc
    if len(key) != 32:
        raise InvalidKeyLengthError("HMAC key must be 32 bytes")
    return key
