"""Tests for the artifact store (PR8).

Covers bytes/Path round-trips, sha256 correctness, MIME detection and
fallback, content dedupe, duplicate-ID rejection, not-found failures,
sorted listing, timestamp validation, atomic index updates, corrupt
index handling, and deletion. Every test uses its own ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.artifacts import (
    ArtifactExistsError,
    ArtifactIndexError,
    ArtifactNotFoundError,
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
)

# sha256(b"hello")
_HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

_T1 = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)


def _read_index(store: ArtifactStore) -> dict[str, object]:
    """Parse the store's index file as JSON (must always be valid)."""
    return json.loads(store.index_path.read_text(encoding="utf-8"))


def test_for_run_points_at_state_artifacts(tmp_path: Path) -> None:
    """The standard run store lives at ``state_dir / 'artifacts'``."""
    store = ArtifactStore.for_run(tmp_path)
    assert store.root == tmp_path / "artifacts"
    assert store.root.is_dir()


@pytest.mark.asyncio
async def test_put_get_roundtrip_bytes(tmp_path: Path) -> None:
    """Bytes source: content file, hash, size, and record round-trip."""
    store = ArtifactStore(tmp_path)
    record = await store.put(source=b"hello", artifact_id="art-1", mime_type="text/plain", at=_T1)
    assert record.artifact_id == "art-1"
    assert record.hash == _HELLO_SHA256
    assert record.mime_type == "text/plain"
    assert record.size == 5
    assert record.created_at == _T1
    assert record.relative_path == "art-1"

    fetched = await store.get("art-1")
    assert fetched == record
    assert (store.root / "art-1").read_bytes() == b"hello"
    assert store.path_for("art-1") == tmp_path / "art-1"


@pytest.mark.asyncio
async def test_put_roundtrip_path_source(tmp_path: Path) -> None:
    """A Path source is read and stored identically to bytes."""
    source = tmp_path / "scan.txt"
    source.write_text("nmap -sV\n", encoding="utf-8")
    store = ArtifactStore(tmp_path)
    record = await store.put(source=source, artifact_id="art-scan")
    assert record.size == source.stat().st_size
    assert len(record.hash) == 64
    assert (store.root / "art-scan").read_bytes() == source.read_bytes()
    assert (await store.get("art-scan")).size == record.size


@pytest.mark.asyncio
async def test_sha256_known_vector(tmp_path: Path) -> None:
    """The stored hash matches the well-known sha256 vector for ``hello``."""
    store = ArtifactStore(tmp_path)
    record = await store.put(source=b"hello")
    assert record.hash == _HELLO_SHA256
    # Content-addressed: the ID is the sha256 digest when none is given.
    assert record.artifact_id == _HELLO_SHA256
    assert store.path_for(record.artifact_id) == tmp_path / _HELLO_SHA256


@pytest.mark.asyncio
async def test_mime_detection_and_fallback(tmp_path: Path) -> None:
    """MIME is detected from Path names, explicit values win, and
    unknown/bytes sources fall back to octet-stream."""
    store = ArtifactStore(tmp_path)
    (tmp_path / "report.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "rawoutput").write_text("x", encoding="utf-8")
    txt = await store.put(source=tmp_path / "report.txt", artifact_id="a")
    assert txt.mime_type == "text/plain"
    html = await store.put(source=tmp_path / "page.html", artifact_id="b")
    assert html.mime_type == "text/html"
    explicit = await store.put(source=b"x", artifact_id="c", mime_type="application/json")
    assert explicit.mime_type == "application/json"
    fallback_ext = await store.put(source=tmp_path / "rawoutput", artifact_id="d")
    assert fallback_ext.mime_type == "application/octet-stream"
    fallback_bytes = await store.put(source=b"y", artifact_id="e")
    assert fallback_bytes.mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_dedupe_returns_existing_record(tmp_path: Path) -> None:
    """Putting identical content under an existing ID returns the
    original record and leaves exactly one content file."""
    store = ArtifactStore(tmp_path)
    first = await store.put(source=b"same", artifact_id="art-1", at=_T1)
    second = await store.put(source=b"same", artifact_id="art-1", at=_T1)
    assert second == first
    assert second.artifact_id == "art-1"
    entries = await store.list()
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_duplicate_id_with_different_content_raises(tmp_path: Path) -> None:
    """An existing ID with different content is rejected loudly."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"one", artifact_id="art-1")
    with pytest.raises(ArtifactExistsError):
        await store.put(source=b"two", artifact_id="art-1")
    # The original content is untouched.
    assert (store.root / "art-1").read_bytes() == b"one"


@pytest.mark.asyncio
async def test_get_missing_raises(tmp_path: Path) -> None:
    """get() on an unknown ID raises ArtifactNotFoundError."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"x", artifact_id="art-1")
    with pytest.raises(ArtifactNotFoundError):
        await store.get("ghost")


@pytest.mark.asyncio
async def test_path_for_missing_raises(tmp_path: Path) -> None:
    """path_for() on an unknown ID raises ArtifactNotFoundError."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"x", artifact_id="art-1")  # create the index
    with pytest.raises(ArtifactNotFoundError):
        store.path_for("ghost")


@pytest.mark.asyncio
async def test_delete_missing_raises(tmp_path: Path) -> None:
    """delete() on an unknown ID raises ArtifactNotFoundError."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"x", artifact_id="art-1")  # create the index
    with pytest.raises(ArtifactNotFoundError):
        await store.delete("ghost")


@pytest.mark.asyncio
async def test_list_sorted_by_created_at(tmp_path: Path) -> None:
    """list() returns records ordered by creation time (then ID)."""
    store = ArtifactStore(tmp_path)
    later = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
    await store.put(source=b"b", artifact_id="mid", at=_T1)
    await store.put(source=b"c", artifact_id="early", at=datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC))
    await store.put(source=b"a", artifact_id="late", at=later)
    ids = [record.artifact_id for record in await store.list()]
    assert ids == ["early", "mid", "late"]


@pytest.mark.asyncio
async def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    """A naive creation timestamp is rejected loudly."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"seed", artifact_id="seed")  # create the index
    with pytest.raises(ValueError):
        await store.put(
            source=b"x",
            artifact_id="art-1",
            at=datetime(2026, 8, 6, 10, 0, 0),  # noqa: DTZ001 - deliberately naive
        )
    # Nothing was written for the rejected put.
    with pytest.raises(ArtifactNotFoundError):
        await store.get("art-1")


@pytest.mark.asyncio
async def test_timestamp_normalized_to_utc(tmp_path: Path) -> None:
    """A non-UTC offset is normalized to UTC on storage."""
    store = ArtifactStore(tmp_path)
    offset = timezone(timedelta(hours=2))
    record = await store.put(
        source=b"x", artifact_id="art-1", at=datetime(2026, 8, 6, 12, 0, 0, tzinfo=offset)
    )
    assert record.created_at.utcoffset() == timedelta(0)
    assert record.created_at == datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_index_always_valid_json_after_puts(tmp_path: Path) -> None:
    """After multiple puts the index file parses as valid JSON and
    matches the store's records (atomic temp-file + replace updates)."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"one", artifact_id="a")
    await store.put(source=b"two", artifact_id="b")
    index = _read_index(store)
    assert set(index) == {"a", "b"}
    entry = index["a"]
    assert isinstance(entry, dict)
    assert entry["mime_type"] == "application/octet-stream"
    second = index["b"]
    assert isinstance(second, dict)
    assert second["size"] == 3
    # No temp file is left behind.
    assert not (store.root / "artifacts.json.tmp").exists()


@pytest.mark.asyncio
async def test_corrupt_index_raises(tmp_path: Path) -> None:
    """A corrupt index file raises ArtifactIndexError on every read and
    is never silently recreated."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"x", artifact_id="a")
    store.index_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactIndexError):
        await store.get("a")
    with pytest.raises(ArtifactIndexError):
        await store.list()
    with pytest.raises(ArtifactIndexError):
        await store.delete("a")
    with pytest.raises(ArtifactIndexError):
        await store.put(source=b"y", artifact_id="b")
    # The corrupt file was not replaced.
    assert store.index_path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.asyncio
async def test_missing_index_raises_on_read(tmp_path: Path) -> None:
    """Reading from a store that never wrote an index is a loud
    ArtifactIndexError (no recreate-on-demand)."""
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactIndexError):
        await store.get("a")
    with pytest.raises(ArtifactIndexError):
        await store.list()
    with pytest.raises(ArtifactIndexError):
        store.path_for("a")


@pytest.mark.asyncio
async def test_invalid_index_entry_raises(tmp_path: Path) -> None:
    """An index entry that is not a valid ArtifactRecord is corruption."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"x", artifact_id="a")
    store.index_path.write_text('{"a": {"artifact_id": "a"}}', encoding="utf-8")
    with pytest.raises(ArtifactIndexError):
        await store.get("a")


@pytest.mark.asyncio
async def test_delete_removes_file_and_entry(tmp_path: Path) -> None:
    """delete() removes both the content file and the index entry."""
    store = ArtifactStore(tmp_path)
    await store.put(source=b"gone", artifact_id="a")
    assert (store.root / "a").is_file()
    await store.delete("a")
    assert not (store.root / "a").exists()
    with pytest.raises(ArtifactNotFoundError):
        await store.get("a")
    assert "a" not in _read_index(store)


@pytest.mark.asyncio
async def test_invalid_artifact_id_rejected(tmp_path: Path) -> None:
    """Path-traversal IDs are rejected loudly."""
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactStoreError):
        await store.put(source=b"x", artifact_id="../escape")
    with pytest.raises(ArtifactStoreError):
        await store.put(source=b"x", artifact_id="a/b")


@pytest.mark.asyncio
async def test_record_validator_rejects_naive_directly() -> None:
    """ArtifactRecord itself rejects naive timestamps (ValidationError)."""
    with pytest.raises(ValidationError):
        ArtifactRecord(
            artifact_id="x",
            hash="h",
            mime_type="text/plain",
            size=1,
            created_at=datetime(2026, 8, 6, 10, 0, 0),  # noqa: DTZ001 - deliberately naive
            relative_path="x",
        )
