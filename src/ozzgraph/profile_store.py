"""ProfileStore: deterministic, TOML-backed per-model profiles (V05).

Implements the empirical model-profiles half of docs/CHANGES_v2.md
milestone 5 (\"Model profiles empirical\"): per-model TOML profiles with
measured capabilities, chosen from data instead of hardcoded family
assumptions.

- Data, not code: profiles live as ``*.toml`` files in a data
  directory (the shipped set ships under ``profile_data/`` as wheel
  package data). :class:`ProfileStore` loads them deterministically —
  sorted filename order, identical registry for identical input.

- Discovery from data: :meth:`ProfileStore.discover` selects a profile
  by exact per-model id, then family prefix, then the conservative
  fallback, and refines it with evidence (the provider's advertised
  model list from ``GET /v1/models`` and one capability-probe
  completion classified with :func:`~ozzgraph.profiles.probe_protocol`).
  :meth:`ProfileStore.discover_from_service` drives the full flow
  against a :class:`~ozzgraph.model_client.ModelService`: unknown
  models are probed, never assumed.

- Benchmark persistence: :meth:`ProfileStore.update_benchmarks`
  persists measured model-harness metrics
  (:class:`~ozzgraph.traces.TraceMetrics` — format compliance, tool
  selection, repetition, evidence grounding, solve rates) back into
  the TOML files as ``[benchmarks.<protocol>]`` tables, and
  :meth:`ProfileStore.persist_report` wires a whole
  :class:`~ozzgraph.matrix.MatrixReport` into the store. Writes are
  byte-deterministic: the same input always produces the same file.

Design rules (AGENTS.md):

- Deterministic and pure: loading and discovery do no network I/O (and
  none at import time — a store is constructed explicitly, never at
  module import); the same directory always yields the same registry
  and the same discovery result.
- Fail loudly (AGENTS.md rule #9): a missing data dir, a duplicate
  family, a corrupt TOML file, or an unwritable data dir all raise —
  never silently fall back.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from ozzgraph.matrix import MATRIX_PROBE_PROMPT, MatrixReport
from ozzgraph.model_client import ModelMessage, ModelRequest, ModelService
from ozzgraph.profiles import (
    PROBE_CONFIDENCE_THRESHOLD,
    ModelProfile,
    default_profile_dir,
    load_profiles_from_dir,
    profile_for_model_id,
    profile_from_toml_mapping,
    refine_profile,
)
from ozzgraph.traces import TraceMetrics

#: Field order of the profile table in a written TOML file (stable
#: output, AGENTS.md: prefer deterministic code).
_PROFILE_FIELD_ORDER: tuple[str, ...] = (
    "family",
    "model_ids",
    "protocols",
    "context_soft_limit",
    "output_token_limit",
    "temperature",
    "supported_roles",
    "max_advertised_skills",
    "failure_behavior",
    "confidence",
)

#: Field order of a benchmark (TraceMetrics) table in a written TOML
#: file. ``model_dump`` already emits declaration order, but the order
#: is pinned here so the serializer never depends on pydantic layout.
_METRIC_FIELD_ORDER: tuple[str, ...] = (
    "valid_output_rate",
    "correct_tool_selection",
    "repetition_rate",
    "recovery_rate",
    "output_tokens_per_decision",
    "steps_per_objective",
    "solve_rate",
    "unsupported_fact_rate",
    "unsupported_flag_rate",
)

#: Header emitted at the top of every file the store writes.
_GENERATED_HEADER = (
    "# OzzGraph model profile (V05, written by ProfileStore).",
    "# Benchmarks are measured harness metrics (TraceMetrics) per protocol.",
)


class ProfileStoreError(RuntimeError):
    """Base error for the profile store (AGENTS.md rule #9)."""


class ProfileStore:
    """A deterministic TOML-backed store of per-model profiles.

    Args:
        data_dir: Directory of ``*.toml`` profile files. Defaults to
            the shipped package data directory
            (:func:`~ozzgraph.profiles.default_profile_dir`), which is
            the read-only source of the initial profiles. Point a
            store at a writable directory (e.g. a user data dir seeded
            from the shipped files) to persist measured benchmarks.
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._dir = Path(data_dir) if data_dir is not None else default_profile_dir()
        self._profiles, self._model_ids = self._load()

    @property
    def data_dir(self) -> Path:
        """The directory this store reads (and writes) profiles from."""
        return self._dir

    def _load(self) -> tuple[dict[str, ModelProfile], dict[str, str]]:
        """Load the registry and the exact per-model id index."""
        profiles = load_profiles_from_dir(self._dir)
        model_ids: dict[str, str] = {}
        for family, profile in profiles.items():
            for model_id in profile.model_ids:
                model_ids[model_id.lower()] = family
        return profiles, model_ids

    def registry(self) -> dict[str, ModelProfile]:
        """A copy of the family -> profile registry (never mutated by callers)."""
        return dict(self._profiles)

    def families(self) -> tuple[str, ...]:
        """The registered family names, in sorted order (deterministic)."""
        return tuple(sorted(self._profiles))

    def profile_for(self, model_id: str) -> ModelProfile:
        """Select a profile from store data for ``model_id`` (deterministic).

        Resolution order: an exact per-model id declared in a profile's
        ``model_ids`` (case-insensitive), then the family-prefix match
        (``"gpt-4o"`` -> ``gpt``), then the conservative fallback
        profile. Unknown ids resolve to the low-confidence fallback,
        which keeps protocol probing in the discovery loop — never
        assumed.
        """
        lowered = model_id.lower()
        family = self._model_ids.get(lowered)
        if family is not None:
            return self._profiles[family]
        return profile_for_model_id(model_id, profiles=self._profiles)

    def discover(
        self,
        model_id: str,
        *,
        sample: str | None = None,
        models: Sequence[str] | None = None,
    ) -> ModelProfile:
        """Select a profile from store data, refining it with any evidence.

        Bases on :meth:`profile_for` (data-driven selection) and applies
        the same evidence rules as
        :func:`~ozzgraph.profiles.discover_profile`: an advertised
        model list (``GET /v1/models``) and one capability-probe
        completion (classified with
        :func:`~ozzgraph.profiles.probe_protocol`) refine low-confidence
        profiles. Unknown models are probed, not assumed — the
        function-call protocol is never added from a text sample.
        """
        return refine_profile(self.profile_for(model_id), model_id, sample=sample, models=models)

    async def discover_from_service(self, service: ModelService, model_id: str) -> ModelProfile:
        """Discover from data: ``GET /v1/models`` + one capability probe.

        Lists the provider's advertised models via
        :meth:`ModelService.list_models` and — only when the store's
        base profile for ``model_id`` is low-confidence (an unknown
        model) — runs one probe completion, classified by
        :func:`~ozzgraph.profiles.probe_protocol` inside
        :meth:`discover`. Known families stand on their data; unknown
        models are probed, never assumed.

        Raises:
            ModelServiceError: When the provider fails (propagated
                loudly — discovery never silently degrades).
        """
        models = [info.id for info in await service.list_models()]
        sample: str | None = None
        if self.profile_for(model_id).confidence < PROBE_CONFIDENCE_THRESHOLD:
            response = await service.complete(
                ModelRequest(
                    model=model_id,
                    messages=[ModelMessage(role="user", content=MATRIX_PROBE_PROMPT)],
                )
            )
            sample = response.choices[0].message.content or ""
        return self.discover(model_id, models=models, sample=sample)

    def update_benchmarks(self, model_id: str, protocol: str, metrics: TraceMetrics) -> None:
        """Persist measured harness metrics into the profile's TOML file.

        Resolves ``model_id`` to its family profile file (exact
        per-model id, then family prefix, then fallback) and writes
        ``[benchmarks.<protocol>]`` into that file, keeping the
        in-memory registry in sync. The write is byte-deterministic:
        the same input always produces the same file.

        Raises:
            ProfileStoreError: When the store directory is not
                writable (fail loudly — a benchmark that cannot be
                persisted must never be silently dropped).
        """
        family = self.profile_for(model_id).family
        path = self._dir / f"{family}.toml"
        data: dict[str, object]
        if path.exists():
            data = tomllib.loads(path.read_text())
        else:
            data = _profile_to_mapping(self.profile_for(model_id))
        benchmarks = data.get("benchmarks")
        if not isinstance(benchmarks, dict):
            benchmarks = {}
            data["benchmarks"] = benchmarks
        benchmarks[protocol] = metrics.model_dump()
        try:
            path.write_text(_dump_toml(data))
        except OSError as exc:
            raise ProfileStoreError(f"cannot persist benchmarks to {path}: {exc}") from exc
        self._profiles[family] = profile_from_toml_mapping(data)
        for declared in self._profiles[family].model_ids:
            self._model_ids[declared.lower()] = family

    def persist_report(self, report: MatrixReport) -> None:
        """Persist every measured protocol row of a harness report.

        Wires the model-harness matrix output back into the store:
        each :class:`~ozzgraph.matrix.MatrixRow`'s
        :class:`~ozzgraph.traces.TraceMetrics` (format compliance, tool
        selection, repetition, evidence grounding, solve rates) is
        written as the profile's ``[benchmarks.<protocol>]`` table.
        """
        for row in report.rows:
            self.update_benchmarks(report.model_id, row.protocol, row.metrics)


def _profile_to_mapping(profile: ModelProfile) -> dict[str, object]:
    """The profile as a TOML-mapping (fields + empty benchmarks table)."""
    data: dict[str, object] = profile.model_dump()
    if not isinstance(data.get("benchmarks"), dict):
        data["benchmarks"] = {}
    return data


def _toml_scalar(value: object) -> str:
    """Serialize one scalar or list-of-scalars as a TOML value.

    JSON syntax is a subset of TOML for the types this store writes
    (strings, numbers, booleans, arrays of those), so ``json.dumps``
    yields valid, deterministic TOML. None is never written (fields
    are omitted instead). Anything else fails loudly — a field the
    serializer cannot represent must never be silently dropped.
    """
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, (list, tuple, frozenset)):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML")


def _dump_toml(data: Mapping[str, object]) -> str:
    """Deterministically render a profile mapping as TOML text.

    Profile fields are emitted in :data:`_PROFILE_FIELD_ORDER` (None
    fields omitted, matching their omitted default on reload);
    benchmark tables are emitted in sorted protocol order with metrics
    in :data:`_METRIC_FIELD_ORDER`. The same input always produces the
    same bytes.
    """
    lines: list[str] = list(_GENERATED_HEADER)
    for key in _PROFILE_FIELD_ORDER:
        value = data.get(key)
        if value is None:
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return "\n".join(lines) + "\n"
    for protocol in sorted(benchmarks):
        table = benchmarks[protocol]
        if not isinstance(table, dict):
            raise ProfileStoreError(f"benchmark table {protocol!r} is not a mapping")
        lines.append(f"[benchmarks.{protocol}]")
        for metric in _METRIC_FIELD_ORDER:
            lines.append(f"{metric} = {_toml_scalar(table[metric])}")
    return "\n".join(lines) + "\n"
