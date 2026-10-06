"""Provider-independent immutable checkpoint publication and reconstruction.

Applications validate identity, select content and bind callbacks to their own
storage scope. Callbacks must implement create-only immutable writes and an
atomic head comparison against the supplied opaque token. An upload can leave
unreferenced immutable objects; it never acknowledges a failed head update.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import re
import zlib

from .filesystem import digest

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class CheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class Publication:
    snapshot: str
    changed: bool
    uploaded_bytes: int


def validate_file_entry(entry, chunk_bytes):
    """Validate content metadata; paths, modes and project authority remain local."""
    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) or not _DIGEST.fullmatch(entry["sha256"]):
        raise CheckpointError("Invalid file digest")
    if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
        raise CheckpointError("Invalid file size or mode")
    chunks = entry.get("chunks")
    if not isinstance(chunks, list):
        raise CheckpointError("Invalid chunk collection")
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("sha256"), str) or not _DIGEST.fullmatch(chunk["sha256"]):
            raise CheckpointError("Invalid chunk digest")
        if type(chunk.get("bytes")) is not int or not 0 < chunk["bytes"] <= chunk_bytes:
            raise CheckpointError("Invalid chunk size")
    if sum(chunk["bytes"] for chunk in chunks) != entry["bytes"]:
        raise CheckpointError("Checkpoint file size differs from its chunks")


def _chunk_sizes(files, chunk_bytes):
    sizes = {}
    for entry in files.values():
        validate_file_entry(entry, chunk_bytes)
        for chunk in entry["chunks"]:
            sha, size = chunk["sha256"], chunk["bytes"]
            if sha in sizes and sizes[sha] != size:
                raise CheckpointError("A checkpoint chunk has conflicting sizes")
            sizes[sha] = size
    return sizes


def verified_chunk(compressed, sha, size, chunk_bytes):
    """Bound compressed input and expansion, then verify exact bytes and digest."""
    if (not isinstance(compressed, bytes) or len(compressed) > chunk_bytes + 65536
        or not isinstance(sha, str) or not _DIGEST.fullmatch(sha)
        or type(size) is not int or not 0 < size <= chunk_bytes):
        raise CheckpointError("Checkpoint chunk failed verification")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
            data = archive.read(size + 1)
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise CheckpointError("Checkpoint chunk failed verification") from error
    if len(data) != size or digest(data) != sha:
        raise CheckpointError("Checkpoint chunk failed verification")
    return data


def publish_snapshot(snapshot_bytes, previous_files, *, current, base,
                     token, staged_chunk, immutable_blob, immutable_snapshot,
                     advance_head, head_bytes, chunk_bytes,
                     max_manifest_bytes=32 * 1024 * 1024):
    """Publish verified immutable content before one conditional head update.

    ``advance_head(head_bytes, token)`` must create exclusively when token is
    None, and compare against the exact nonempty token otherwise. Provider
    conflicts and lost responses propagate: callers acknowledge only a returned
    Publication, and explicitly reread/reconcile an ambiguous response.
    """
    if not isinstance(snapshot_bytes, bytes) or len(snapshot_bytes) > max_manifest_bytes:
        raise CheckpointError("Checkpoint manifest is too large")
    sha = digest(snapshot_bytes)
    if current == sha:
        return Publication(sha, False, 0)
    if current != base:
        raise CheckpointError("Cloud workspace changed since this machine's last checkpoint; restore and reconcile before publishing")
    if ((current is None and token is not None)
        or (current is not None and (not isinstance(token, str) or not token))):
        raise CheckpointError("Checkpoint head lacks an exact comparison token")
    try:
        files = json.loads(snapshot_bytes)["files"]
        head = json.loads(head_bytes)
        if not isinstance(files, dict) or head["snapshot"] != sha or head["previous"] != current:
            raise ValueError("Head differs from immutable snapshot")
    except (ValueError, TypeError, KeyError) as error:
        raise CheckpointError("Checkpoint head differs from its immutable snapshot") from error
    sizes = _chunk_sizes(files, chunk_bytes)
    previous_sizes = _chunk_sizes(previous_files, chunk_bytes)
    if any(sha in previous_sizes and previous_sizes[sha] != size for sha, size in sizes.items()):
        raise CheckpointError("A checkpoint chunk has conflicting sizes")

    def upload(chunk_sha):
        content = staged_chunk(chunk_sha, chunk_bytes + 65536)
        verified_chunk(content, chunk_sha, sizes[chunk_sha], chunk_bytes)
        immutable_blob(chunk_sha, content)
        return len(content)

    with ThreadPoolExecutor(max_workers=4) as executor:
        uploaded = sum(executor.map(upload, sorted(sizes.keys() - previous_sizes.keys())))
    immutable_snapshot(sha, snapshot_bytes)
    advance_head(head_bytes, token)
    return Publication(sha, True, uploaded)


def reconstruct_file(entry, stream, load_chunk, *, chunk_bytes):
    """Reconstruct into an app-owned staging stream, checking the whole file."""
    validate_file_entry(entry, chunk_bytes)
    sha256 = hashlib.sha256()
    size = 0
    for chunk in entry["chunks"]:
        content = load_chunk(chunk["sha256"], chunk_bytes + 65536)
        data = verified_chunk(content, chunk["sha256"], chunk["bytes"], chunk_bytes)
        stream.write(data)
        sha256.update(data)
        size += len(data)
    if size != entry["bytes"] or sha256.hexdigest() != entry["sha256"]:
        raise CheckpointError("Restored file failed verification")
