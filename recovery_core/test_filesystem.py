import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from . import filesystem as fs


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_scoped_directory_rejects_traversal_and_linked_parents(self):
        for name in ('..', '../other', '/other', 'one/two', 'one\\two', ''):
            with self.assertRaises(ValueError):
                fs.scoped_directory(self.root, name, create=True)
        (self.root / 'linked').symlink_to(self.root)
        with self.assertRaises(ValueError):
            fs.scoped_directory(self.root, 'linked', 'notes', create=True)
        target = fs.scoped_directory(self.root, 'context', 'notes', create=True)
        self.assertEqual(target, self.root / 'context/notes')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_bounded_read_rejects_links_and_fifo_without_waiting(self):
        source = self.root / 'note'; source.write_bytes(b'123456')
        self.assertEqual(fs.read_regular_bytes(source, 3), b'1234')
        linked = self.root / 'linked'; linked.symlink_to(source)
        with self.assertRaises(OSError):
            fs.read_regular_bytes(linked, 10)
        pipe = self.root / 'pipe'; os.mkfifo(pipe)
        with self.assertRaises(fs.RegularFileRequired):
            fs.read_regular_bytes(pipe, 10)

    def test_changed_open_file_is_not_accepted(self):
        class ChangingStream:
            def fileno(self): return 10
            def read(self, count): return b'content'
        original = os.stat(self.root)
        changed = type('Stat', (), {field: getattr(original, field) for field in
                    ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})()
        changed.st_ctime_ns += 1
        original_file = type('Stat', (), {field: getattr(original, field) for field in
                        ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})()
        original_file.st_mode = stat.S_IFREG | 0o600
        with patch.object(fs.os, 'fstat', side_effect=[original_file, changed]):
            with self.assertRaises(fs.FileChangedDuringRead):
                fs._read_stream(ChangingStream(), 10)

    def test_immutable_retry_preserves_bytes_and_conflict_is_refused(self):
        target = self.root / 'evidence.json'
        fs.immutable_bytes(target, b'original')
        first = target.stat()
        fs.immutable_bytes(target, b'original')
        self.assertEqual(target.stat().st_ino, first.st_ino)
        self.assertEqual(stat.S_IMODE(first.st_mode), 0o600)
        with self.assertRaises(fs.ExistingContentDiffers):
            fs.immutable_bytes(target, b'changed')
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.root.glob('.recovery-*')), [])

    def test_failed_publication_leaves_no_partial_evidence(self):
        target = self.root / 'evidence.json'
        with patch.object(fs.os, 'link', side_effect=OSError('publication interrupted')):
            with self.assertRaises(OSError):
                fs.immutable_bytes(target, b'complete content')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_link_or_fifo_is_never_replaced(self):
        source = self.root / 'source'; source.write_bytes(b'original')
        for name in ('linked', 'pipe'):
            target = self.root / name
            if name == 'linked': target.symlink_to(source)
            else: os.mkfifo(target)
            with self.assertRaises(fs.ExistingContentDiffers):
                fs.immutable_bytes(target, b'original')
        self.assertTrue((self.root / 'linked').is_symlink())
        self.assertTrue(stat.S_ISFIFO((self.root / 'pipe').stat().st_mode))
        self.assertEqual(source.read_bytes(), b'original')

    def test_cooperative_lock_blocks_other_process_and_releases_on_error(self):
        target = self.root / 'lock'
        child = ('import fcntl,sys; stream=open(sys.argv[1],"ab"); '
                 '\ntry: fcntl.flock(stream, fcntl.LOCK_EX|fcntl.LOCK_NB)'
                 '\nexcept BlockingIOError: sys.exit(7)')
        def attempt():
            return subprocess.run([sys.executable, '-B', '-c', child, str(target)], timeout=5).returncode
        with self.assertRaises(RuntimeError):
            with fs.exclusive_file_lock(target):
                self.assertEqual(attempt(), 7)
                raise RuntimeError('operator interruption')
        self.assertEqual(attempt(), 0)


if __name__ == '__main__':
    unittest.main()
