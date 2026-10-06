import gzip
import io
import json
import unittest

from .checkpoints import CheckpointError, publish_snapshot, reconstruct_file, verified_chunk
from .filesystem import digest


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def entry(*parts):
    return {"sha256": digest(b"".join(parts)), "bytes": sum(map(len, parts)),
            "executable": False,
            "chunks": [{"sha256": digest(part), "bytes": len(part)} for part in parts]}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.parts = {digest(value): gzip.compress(value, mtime=0) for value in (b"one", b"two")}
        self.files = {"workspace/a": entry(b"one", b"two"), "workspace/b": entry(b"one")}
        self.data = encoded({"files": self.files})
        self.sha = digest(self.data)

    def publish(self, **changes):
        options = {"current": None, "base": None, "token": None,
                   "staged_chunk": lambda sha, maximum: self.parts[sha],
                   "immutable_blob": lambda sha, data: self.events.append(("blob", sha, data)),
                   "immutable_snapshot": lambda sha, data: self.events.append(("snapshot", sha, data)),
                   "advance_head": lambda data, token: self.events.append(("head", data, token)),
                   "head_bytes": encoded({"snapshot": self.sha, "previous": None}), "chunk_bytes": 64}
        options.update(changes)
        return publish_snapshot(self.data, {}, **options)

    def test_immutable_content_precedes_create_only_head_and_duplicates_upload_once(self):
        result = self.publish()
        self.assertEqual(result.snapshot, self.sha)
        self.assertTrue(result.changed)
        self.assertEqual(result.uploaded_bytes, sum(map(len, self.parts.values())))
        self.assertEqual(len([e for e in self.events if e[0] == "blob"]), 2)
        self.assertEqual([e[0] for e in self.events][-2:], ["snapshot", "head"])
        self.assertIsNone(self.events[-1][2])

    def test_exact_observed_token_and_prior_chunks_are_preserved(self):
        token = '"opaque-revision-token"'
        result = publish_snapshot(self.data, {"old": entry(b"one")}, current="a" * 64,
            base="a" * 64, token=token, staged_chunk=lambda sha, maximum: self.parts[sha],
            immutable_blob=lambda sha, data: self.events.append(("blob", sha)),
            immutable_snapshot=lambda sha, data: self.events.append(("snapshot", sha)),
            advance_head=lambda data, value: self.events.append(("head", value)),
            head_bytes=encoded({"snapshot": self.sha, "previous": "a" * 64}), chunk_bytes=64)
        self.assertTrue(result.changed)
        self.assertEqual([e[1] for e in self.events if e[0] == "blob"], [digest(b"two")])
        self.assertEqual(self.events[-1], ("head", token))

    def test_identical_retry_does_not_read_or_write_even_with_old_local_base(self):
        result = self.publish(current=self.sha, base="a" * 64)
        self.assertFalse(result.changed)
        self.assertEqual(self.events, [])

    def test_stale_baseline_and_missing_head_token_refuse_all_writes(self):
        with self.assertRaisesRegex(CheckpointError, "Cloud workspace changed"):
            self.publish(current="a" * 64)
        with self.assertRaisesRegex(CheckpointError, "comparison token"):
            self.publish(current="a" * 64, base="a" * 64)
        self.assertEqual(self.events, [])

    def test_conflict_or_lost_response_cannot_return_success(self):
        for reason in ("conflict", "response lost"):
            def failed_head(data, token):
                raise RuntimeError(reason)
            with self.assertRaisesRegex(RuntimeError, reason):
                self.publish(advance_head=failed_head)
            self.assertEqual(self.events[-1][0], "snapshot")
            self.events.clear()

    def test_corrupt_staged_chunk_cannot_publish_a_snapshot_or_head(self):
        for bad in (gzip.compress(b"wrong", mtime=0), gzip.compress(b"one", mtime=0) + b"junk", b"not gzip"):
            with self.assertRaisesRegex(CheckpointError, "chunk failed verification"):
                self.publish(staged_chunk=lambda sha, maximum: bad)
            self.assertFalse(any(e[0] in ("snapshot", "head") for e in self.events))
            self.events.clear()

    def test_declared_size_and_decompression_expansion_are_bounded(self):
        for bad, sha, size in ((gzip.compress(b"one"), digest(b"one"), 2),
                               (gzip.compress(b"x" * 2_000_000), digest(b"x"), 1),
                               (b"x" * (64 + 65537), digest(b"x"), 1)):
            with self.assertRaises(CheckpointError):
                verified_chunk(bad, sha, size, 64)

    def test_full_file_digest_and_empty_file_are_verified(self):
        output = io.BytesIO()
        reconstruct_file(entry(b"one", b"two"), output, lambda sha, maximum: self.parts[sha], chunk_bytes=64)
        self.assertEqual(output.getvalue(), b"onetwo")
        reconstruct_file(entry(), io.BytesIO(), lambda *args: self.fail("Empty file loaded a chunk"), chunk_bytes=64)
        bad = entry(b"one")
        bad["sha256"] = digest(b"two")
        with self.assertRaisesRegex(CheckpointError, "file failed verification"):
            reconstruct_file(bad, io.BytesIO(), lambda sha, maximum: self.parts[sha], chunk_bytes=64)

    def test_same_chunk_digest_with_conflicting_sizes_is_rejected(self):
        bad = entry(b"one")
        bad["chunks"][0]["bytes"] = 2
        bad["bytes"] = 2
        data = encoded({"files": {**self.files, "bad": bad}})
        with self.assertRaisesRegex(CheckpointError, "conflicting sizes"):
            publish_snapshot(data, {}, current=None, base=None, token=None,
                staged_chunk=lambda *args: self.fail("Read before validation"),
                immutable_blob=lambda *args: self.fail("Write before validation"),
                immutable_snapshot=lambda *args: self.fail("Write before validation"),
                advance_head=lambda *args: self.fail("Write before validation"),
                head_bytes=encoded({"snapshot": digest(data), "previous": None}), chunk_bytes=64)

    def test_head_is_bound_to_the_exact_immutable_snapshot(self):
        with self.assertRaisesRegex(CheckpointError, "differs from its immutable snapshot"):
            self.publish(head_bytes=encoded({"snapshot": "a" * 64, "previous": None}))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
