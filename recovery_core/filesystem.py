"""Bounded reads, create-only evidence and cooperative locks for local recovery.

These helpers neither decide authorization nor call a storage provider. A parent
directory must be controlled by the application; advisory locks serialize users of
this protocol, not unrelated editors. This is not external-filesystem CAS.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4


class RegularFileRequired(ValueError):
    pass


class FileChangedDuringRead(ValueError):
    pass


class ExistingContentDiffers(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def scoped_directory(root, *parts, create=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Root directory is unavailable')
    target = root
    for part in parts:
        if (not isinstance(part, str) or not part or part in ('.', '..')
            or '/' in part or '\\' in part or '\x00' in part):
            raise ValueError('Directory components must be bounded names')
        target /= part
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            if create:
                target.mkdir(mode=0o700)
                mode = target.lstat().st_mode
            else:
                continue
        if not stat.S_ISDIR(mode):
            raise ValueError('Scoped directories must be real directories')
    return target


def _read_stream(stream, max_bytes):
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise RegularFileRequired('Expected a regular file')
    data = stream.read(max_bytes + 1)
    after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise FileChangedDuringRead('File changed while being read')
    return data


def read_regular_bytes(path, max_bytes):
    """Read at most limit+1 bytes; callers reject overflow and apply their policy."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 0 <= max_bytes <= 128 * 1024 * 1024:
        raise ValueError('Use a bounded byte limit')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        return _read_stream(stream, max_bytes)


def immutable_bytes(path, data):
    """Publish complete durable bytes once; identical retries reuse the record."""
    path = Path(path)
    if not isinstance(data, bytes) or len(data) > 128 * 1024 * 1024:
        raise ValueError('Use bounded bytes for an immutable record')
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.recovery-' + uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError:
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                with os.fdopen(descriptor, 'rb') as stream:
                    existing = _read_stream(stream, len(data))
                if existing != data:
                    raise ExistingContentDiffers('Existing immutable content differs')
            except (OSError, RegularFileRequired, FileChangedDuringRead) as error:
                raise ExistingContentDiffers('Existing immutable content differs') from error
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


@contextmanager
def exclusive_file_lock(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RegularFileRequired('Expected a regular lock file')
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
