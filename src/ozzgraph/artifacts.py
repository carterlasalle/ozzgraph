"""Artifact store for OzzGraph (PR8).

Raw artifact bytes live OUTSIDE model context, per AGENTS.md rule #1
("Authoritative state lives outside model context") and
docs/DATA_STRATEGY.md ("Artifact Store"): each artifact is a content file
under ``state_dir/artifacts`` plus one metadata record in the store's
index. Parsers and tool runners store raw output here and return compact
summaries plus artifact handles (docs/ARCHITECTURE.md, "Artifact
Pipeline").

Layout of one store rooted at ``root``:

- ``root/artifacts.json`` — the authoritative metadata index: a JSON
  object mapping artifact ID to the serialized :class:`ArtifactRecord`
  for that artifact. The index is updated atomically (temp file +
  ``os.replace``) and is never rebuilt on demand: a missing or corrupt
  index raises :class:`ArtifactIndexError` (fail loudly, AGENTS.md rule
  #9).
- ``root/<artifact_id>`` — the raw content file. When a caller does not
  supply an ID, the content is stored under its own sha256 hex digest
  (content-addressed), so identical bytes dedupe naturally.

``put`` writes content to a temp file and atomically moves it into
place (``os.replace``), then atomically rewrites the index the same way,
so a crash never leaves a torn content file or a half-written index.
Every failure is reported through the :class:`ArtifactStoreError`
hierarchy; no ``OSError`` or ``json`` exception leaks to callers.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, field_validator

# Name of the authoritative metadata index at the store root.
INDEX_FILE = "artifacts.json"

# MIME fallback when no type can be detected.
_DEFAULT_MIME = "application/octet-stream"


class ArtifactRecord(BaseModel):
    """One artifact's metadata.

    Attributes:
        artifact_id: Unique identifier (uuid4 hex by default).
        hash: sha256 hex digest of the content bytes.
        mime_type: Detected or caller-supplied MIME type.
        size: Content length in bytes.
        source_action: Action ID that produced the artifact, if any.
        target: Target the artifact relates to, if any.
        created_at: Timezone-aware UTC creation timestamp.
        truncated: Whether the captured output was truncated.
        parser_metadata: Parser-specific sidecar data.
        sensitivity: Sensitivity classification (default ``public``).
        relative_path: Content file path relative to the store root.
    """

    artifact_id: str = Field(default_factory=lambda: uuid4().hex)
    hash: str
    mime_type: str
    size: int
    source_action: str | None = None
    target: str | None = None
    created_at: datetime
    truncated: bool = False
    parser_metadata: dict[str, object] = Field(default_factory=dict)
    sensitivity: str = "public"
    relative_path: str

    @field_validator("created_at")
    @classmethod
    def _created_at_must_be_utc(cls, value: datetime) -> datetime:
        """Reject naive timestamps and normalize any offset to UTC.

        Raises:
            ValueError: If the timestamp has no timezone information.
        """
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class ArtifactStoreError(RuntimeError):
    """Base error for every artifact-store failure."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when an artifact is required but does not exist."""


class ArtifactExistsError(ArtifactStoreError):
    """Raised when an artifact ID exists with different content."""


class ArtifactIndexError(ArtifactStoreError):
    """Raised when the metadata index is missing, corrupt, or inconsistent.

    The index is authoritative and is never rebuilt on demand, so a
    missing or unreadable index is a loud failure, never a silent
    re-creation (AGENTS.md rule #9).
    """


class ArtifactStore:
    """Content-addressed artifact store with an authoritative JSON index.

    Args:
        root: Store root directory. Created (recursively) on
            construction when missing.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, state_dir: Path) -> ArtifactStore:
        """The standard run store at ``state_dir / 'artifacts'``."""
        return cls(state_dir / "artifacts")

    @property
    def root(self) -> Path:
        """The store root directory holding the index and content files."""
        return self._root

    @property
    def index_path(self) -> Path:
        """The authoritative metadata index file path."""
        return self._root / INDEX_FILE

    async def put(
        self,
        *,
        source: bytes | Path,
        artifact_id: str | None = None,
        mime_type: str | None = None,
        source_action: str | None = None,
        target: str | None = None,
        truncated: bool = False,
        parser_metadata: dict[str, object] | None = None,
        sensitivity: str = "public",
        at: datetime | None = None,
    ) -> ArtifactRecord:
        """Store raw artifact bytes and index their metadata.

        The content file is written to a temp file and atomically moved
        into place (``os.replace``); the index is then rewritten the same
        way, so readers never observe a torn write. When ``artifact_id``
        is None the content is stored under its own sha256 hex digest.

        Args:
            source: Content bytes, or a path to read content from.
            artifact_id: Optional stable ID. Defaults to the sha256 hex
                digest of the content.
            mime_type: Optional MIME type; otherwise detected from a
                ``Path`` source's name, falling back to
                ``application/octet-stream``.
            source_action: Action ID that produced this artifact.
            target: Target this artifact relates to.
            truncated: Whether captured output was truncated.
            parser_metadata: Parser-specific sidecar metadata.
            sensitivity: Sensitivity classification.
            at: Optional creation timestamp; defaults to UTC now.

        Raises:
            ValueError: If ``at`` is naive (missing timezone info).
            ArtifactExistsError: If ``artifact_id`` already exists with
                different content.
            ArtifactIndexError: If the existing index is corrupt.
            ArtifactStoreError: If the ID is invalid or I/O fails.

        Returns:
            The record of the stored artifact. When the ID already
            exists with identical content, the existing record is
            returned unchanged (dedupe).
        """
        content = source.read_bytes() if isinstance(source, Path) else source
        content_hash = hashlib.sha256(content).hexdigest()
        if artifact_id is None:
            artifact_id = content_hash
        self._validate_artifact_id(artifact_id)
        now = _utc_now(at)
        resolved_mime = self._guess_mime(source, mime_type)

        index = self._load_index(missing_ok=True)
        existing = index.get(artifact_id)
        if existing is not None:
            if existing.hash == content_hash:
                return existing
            raise ArtifactExistsError(
                f"artifact {artifact_id!r} already exists with different content"
            )

        content_path = self._root / artifact_id
        tmp_content = self._root / f".{artifact_id}.tmp"
        try:
            tmp_content.write_bytes(content)
            os.replace(tmp_content, content_path)
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to write artifact content {content_path}: {exc}"
            ) from exc

        record = ArtifactRecord(
            artifact_id=artifact_id,
            hash=content_hash,
            mime_type=resolved_mime,
            size=len(content),
            source_action=source_action,
            target=target,
            created_at=now,
            truncated=truncated,
            parser_metadata={} if parser_metadata is None else parser_metadata,
            sensitivity=sensitivity,
            relative_path=artifact_id,
        )
        index[artifact_id] = record
        self._save_index(index)
        return record

    async def get(self, artifact_id: str) -> ArtifactRecord:
        """Fetch one artifact's metadata by ID.

        Raises:
            ArtifactNotFoundError: If the artifact does not exist.
            ArtifactIndexError: If the index is missing or corrupt.
        """
        index = self._load_index()
        record = index.get(artifact_id)
        if record is None:
            raise ArtifactNotFoundError(f"artifact {artifact_id!r} does not exist")
        return record

    def path_for(self, artifact_id: str) -> Path:
        """The content file path for an artifact.

        Raises:
            ArtifactNotFoundError: If the artifact does not exist.
            ArtifactIndexError: If the index is missing or corrupt.
        """
        index = self._load_index()
        record = index.get(artifact_id)
        if record is None:
            raise ArtifactNotFoundError(f"artifact {artifact_id!r} does not exist")
        return self._root / record.relative_path

    async def list(self) -> list[ArtifactRecord]:
        """Every artifact record, ordered by creation time (then ID).

        Raises:
            ArtifactIndexError: If the index is missing or corrupt.
        """
        index = self._load_index()
        return sorted(index.values(), key=lambda record: (record.created_at, record.artifact_id))

    async def delete(self, artifact_id: str) -> None:
        """Remove an artifact's content file and index entry.

        The index entry is removed and rewritten first, then the content
        file is unlinked, so a crash leaves at worst an orphan content
        file rather than an index entry pointing at nothing.

        Raises:
            ArtifactNotFoundError: If the artifact does not exist.
            ArtifactIndexError: If the index is corrupt or the content
                file is missing.
        """
        index = self._load_index()
        record = index.get(artifact_id)
        if record is None:
            raise ArtifactNotFoundError(f"artifact {artifact_id!r} does not exist")
        content_path = self._root / record.relative_path
        if not content_path.is_file():
            raise ArtifactIndexError(
                f"artifact {artifact_id!r} index entry has no content file at {content_path}"
            )
        del index[artifact_id]
        self._save_index(index)
        try:
            content_path.unlink()
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to remove artifact content {content_path}: {exc}"
            ) from exc

    def _load_index(self, *, missing_ok: bool = False) -> dict[str, ArtifactRecord]:
        """Read and validate the metadata index.

        With ``missing_ok`` (used by :meth:`put`), a store that has never
        written an index is treated as empty; a corrupt index is still a
        loud :class:`ArtifactIndexError`. Reads (``get``/``list``/
        ``delete``) require the index to exist.

        Raises:
            ArtifactIndexError: If the index is missing (when not
                ``missing_ok``), not a JSON object, or holds an invalid
                record.
        """
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            if missing_ok:
                return {}
            raise ArtifactIndexError(f"artifact index missing at {self.index_path}") from exc
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactIndexError(f"artifact index is corrupt: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ArtifactIndexError("artifact index is not a JSON object")
        records: dict[str, ArtifactRecord] = {}
        for key, value in loaded.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ArtifactIndexError(f"artifact index entry {key!r} is malformed")
            try:
                records[key] = ArtifactRecord.model_validate(value)
            except ValidationError as exc:
                raise ArtifactIndexError(f"artifact index entry {key!r} is invalid: {exc}") from exc
        return records

    def _save_index(self, records: dict[str, ArtifactRecord]) -> None:
        """Atomically replace the index with ``records`` (temp + replace)."""
        payload = {aid: record.model_dump(mode="json") for aid, record in records.items()}
        tmp = self._root / f"{INDEX_FILE}.tmp"
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to write artifact index {self.index_path}: {exc}"
            ) from exc

    @staticmethod
    def _validate_artifact_id(artifact_id: str) -> None:
        """Reject IDs that could escape the store root via path traversal."""
        if (
            not artifact_id
            or artifact_id in (".", "..")
            or "/" in artifact_id
            or "\\" in artifact_id
        ):
            raise ArtifactStoreError(f"invalid artifact id {artifact_id!r}")

    @staticmethod
    def _guess_mime(source: bytes | Path, mime_type: str | None) -> str:
        """Explicit MIME wins; then a ``Path`` source's name is probed.

        Falls back to ``application/octet-stream`` when nothing is
        detected (bytes sources have no filename to probe).
        """
        if mime_type is not None:
            return mime_type
        if isinstance(source, Path):
            detected, _ = mimetypes.guess_type(source.name)
            if detected:
                return detected
        return _DEFAULT_MIME


def _utc_now(at: datetime | None) -> datetime:
    """``at`` normalized to UTC, or UTC now when None.

    Raises:
        ValueError: If ``at`` is naive (missing timezone information).
    """
    if at is None:
        return datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return at.astimezone(UTC)
