"""Exercise maintained Git mechanics using disposable local repositories only."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import git as recovery_git


class GitRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-recovery-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def command(self, root, *args):
        return recovery_git.git(root, *args, stdout=subprocess.PIPE)

    def repository(self, name="source with spaces", object_format="sha1"):
        root = self.base / name
        root.mkdir()
        self.command(root, "init", "--initial-branch=main", "--object-format=" + object_format)
        self.command(root, "config", "user.name", "Local Recovery Fixture")
        self.command(root, "config", "user.email", "local@example.invalid")
        (root / "source.txt").write_bytes(b"original source\n")
        (root / "deleted.txt").write_bytes(b"original deleted file\n")
        self.command(root, "add", ".")
        self.command(root, "commit", "-m", "Local original")
        return root

    def bundle(self, root, name="history.bundle", object_format="sha1"):
        manifest = {"object_format": object_format, **recovery_git.capture(root)}
        bundle = self.base / name
        self.command(root, "bundle", "create", str(bundle), "--all", "HEAD")
        return bundle, manifest

    def source_state(self, root):
        files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob("*") if p.is_file()}
        return files, self.command(root, "status", "--porcelain"), recovery_git.capture(root)

    def test_round_trip_retains_all_refs_detached_head_and_original_working_files(self):
        root = self.repository()
        self.command(root, "branch", "local-only")
        self.command(root, "tag", "-a", "release", "-m", "Original release")
        self.command(root, "notes", "add", "-m", "Original provenance")
        (root / "source.txt").write_bytes(b"stashed source\n")
        self.command(root, "stash", "push", "-m", "Original stash")
        self.command(root, "checkout", "--detach")
        (root / "source.txt").write_bytes(b"unsaved current source\n")
        (root / "untracked.txt").write_bytes(b"untracked current source\n")
        (root / "deleted.txt").unlink()
        self.command(root, "config", "remote.original.url", "https://local.invalid/private-source")
        (root / ".git/hooks/post-checkout").write_bytes(b"original local hook\n")
        bundle, manifest = self.bundle(root)
        before = self.source_state(root)
        target = self.base / "reconstructed.git"
        recovery_git.materialize(bundle, target, manifest)
        self.assertEqual(recovery_git.capture(target), {key: manifest[key] for key in ("refs", "head", "head_ref")})
        self.assertEqual(self.command(target, "show", "refs/stash:source.txt"), b"stashed source\n")
        self.assertEqual(self.command(target, "show", "refs/heads/main:source.txt"), b"original source\n")
        self.assertIsNone(manifest["head_ref"])
        self.assertEqual(self.source_state(root), before)
        self.assertFalse((target / "hooks/post-checkout").exists())
        self.assertNotIn("private-source", (target / "config").read_text())

    def test_sha256_repository_retains_symbolic_head_and_exact_object_format(self):
        root = self.repository(object_format="sha256")
        bundle, manifest = self.bundle(root, object_format="sha256")
        target = self.base / "sha256.git"
        recovery_git.materialize(bundle, target, manifest)
        self.assertEqual(recovery_git.capture(target), {key: manifest[key] for key in ("refs", "head", "head_ref")})
        self.assertEqual(manifest["head_ref"], "refs/heads/main")
        self.assertEqual(len(manifest["head"]), 64)
        self.assertEqual(self.command(target, "rev-parse", "--show-object-format"), b"sha256\n")

    def test_corrupt_or_truncated_bundle_cannot_materialize_verified_history(self):
        root = self.repository()
        bundle, manifest = self.bundle(root)
        before = self.source_state(root)
        original = bundle.read_bytes()
        for number, data in enumerate((b"not a bundle", original[:len(original) // 2])):
            with self.subTest(number=number):
                corrupt = self.base / f"corrupt-{number}.bundle"
                corrupt.write_bytes(data)
                with self.assertRaises(recovery_git.GitRecoveryError):
                    recovery_git.materialize(corrupt, self.base / f"bad-{number}.git", manifest)
        self.assertEqual(self.source_state(root), before)

    def test_reference_manifest_mismatch_rejects_otherwise_valid_bundle(self):
        root = self.repository()
        bundle, manifest = self.bundle(root)
        wrong = {**manifest, "refs": {**manifest["refs"], "refs/heads/not-captured": manifest["head"]}}
        with self.assertRaisesRegex(recovery_git.GitRecoveryError, "Recovered Git references do not match the backup"):
            recovery_git.materialize(bundle, self.base / "wrong-refs.git", wrong)

    def test_unborn_head_has_original_safe_failure_message(self):
        root = self.base / "unborn"
        root.mkdir()
        self.command(root, "init", "--initial-branch=main")
        with self.assertRaisesRegex(recovery_git.GitRecoveryError, "Repository has no resolvable HEAD commit; use file sync for uncommitted projects"):
            recovery_git.capture(root)

    def test_inherited_git_config_hooks_and_templates_cannot_change_recovery(self):
        root = self.repository()
        bundle, manifest = self.bundle(root)
        poison = self.base / "poison"
        poison.mkdir()
        (poison / "marker").write_text("Must not become a template")
        configuration = self.base / "global.gitconfig"
        configuration.write_text("[init]\n\ttemplateDir = " + str(poison) + "\n")
        with patch.dict(os.environ, {"GIT_DIR": str(poison), "GIT_WORK_TREE": str(poison),
                "GIT_INDEX_FILE": str(poison / "index"), "GIT_CONFIG_GLOBAL": str(configuration),
                "GIT_TEMPLATE_DIR": str(poison), "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(poison)}):
            self.assertEqual(recovery_git.capture(root), {key: manifest[key] for key in ("refs", "head", "head_ref")})
            target = self.base / "isolated.git"
            recovery_git.materialize(bundle, target, manifest)
        self.assertFalse((target / "marker").exists())
        self.assertFalse((poison / "index").exists())
        self.assertEqual(recovery_git.capture(target), {key: manifest[key] for key in ("refs", "head", "head_ref")})

    def test_nonlocal_transport_is_denied_without_exposing_git_diagnostics(self):
        root = self.repository()
        with self.assertRaises(recovery_git.GitRecoveryError) as failure:
            self.command(root, "ls-remote", "https://local.invalid/private-route")
        self.assertEqual(str(failure.exception), "Git ls-remote failed; no backup was completed")
        self.assertNotIn("private-route", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
