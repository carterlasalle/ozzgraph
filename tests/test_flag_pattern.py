"""HAL-007: HalCTF environment flag-pattern generalization (tests).

Covers the acceptance contract: the HalCTF environment's default flag
pattern generalizes to identifier-style prefixes (``flag{}``,
``HALCTF{}``, ...) independently of the local default; code/CSS brace
blocks never match; an operator-set ``OZZGRAPH_FLAG_PATTERN`` wins over
the HalCTF default; and the local default (``config.flag_pattern`` and
the plain extractor's own default) is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ozzgraph.config import (
    DEFAULT_FLAG_PATTERN,
    FLAG_PATTERN_ENV,
    OzzGraphConfig,
    load_config,
)
from ozzgraph.environments import HalCTFEnvironment
from ozzgraph.environments.halctf import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    ENTITY_FLAG_CANDIDATE,
    HALCTF_DEFAULT_FLAG_PATTERN,
    FlagCandidateExtractor,
)
from ozzgraph.state_graph import StateGraph

ENDPOINT = "http://127.0.0.1:9000/mcp"


def _config(tmp_path: Path, **overrides: object) -> OzzGraphConfig:
    base: dict[str, object] = {
        "hal_user_id": "user-42",
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
        "target_allowlist": ("127.0.0.1",),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type] - test helper


def _halctf_env(**overrides: str) -> dict[str, str]:
    env = {"OZZGRAPH_CHALLENGE_ID": "web-01", "OZZGRAPH_MCP_BASE_URL": ENDPOINT}
    env.update(overrides)
    return env


async def _seed_observed_text(
    graph: StateGraph,
    text: str,
    *,
    observation_id: str = "obs-1",
    evidence_id: str = "ev-1",
) -> None:
    """Seed one evidence-backed observation whose summary is ``text``."""
    await graph.create_entity(observation_id, "observation", {"summary": text})
    await graph.create_entity(evidence_id, "evidence", {"note": "parsed from output"})
    await graph.create_edge(
        f"{evidence_id}-from-{observation_id}",
        EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
        evidence_id,
        observation_id,
    )


# ---------------------------------------------------------------------------
# HalCTF default: generalized identifier-style pattern (HAL-007)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halctf_default_matches_identifier_prefix_flags(tmp_path: Path) -> None:
    """Without an operator pattern, HALCTF{...} AND flag{...} both persist."""
    environment = HalCTFEnvironment(_config(tmp_path), environ=_halctf_env())
    async with StateGraph(":memory:") as graph:
        await _seed_observed_text(graph, "leaked HALCTF{abc_123} and flag{abc}")
        candidates = await environment.flag_extractor(run_id="run-1").extract(graph)

        assert [candidate.flag for candidate in candidates] == [
            "HALCTF{abc_123}",
            "flag{abc}",
        ]
        assert len(await graph.list_entities(ENTITY_FLAG_CANDIDATE)) == 2


@pytest.mark.asyncio
async def test_halctf_default_ignores_js_css_braces(tmp_path: Path) -> None:
    """Code/CSS brace blocks (no identifier immediately before ``{``)
    never match the generalized pattern."""
    environment = HalCTFEnvironment(_config(tmp_path), environ=_halctf_env())
    async with StateGraph(":memory:") as graph:
        await _seed_observed_text(
            graph,
            "function f() { return {a: 1}; } a { color: red; }",
        )
        candidates = await environment.flag_extractor(run_id="run-1").extract(graph)

        assert candidates == ()
        assert await graph.list_entities(ENTITY_FLAG_CANDIDATE) == []


@pytest.mark.asyncio
async def test_halctf_blank_operator_pattern_uses_generalized_default(
    tmp_path: Path,
) -> None:
    """A blank OZZGRAPH_FLAG_PATTERN is unset (load_config's ``_env_str``
    blank-means-unset semantics) — the generalized HalCTF default applies."""
    environment = HalCTFEnvironment(
        _config(tmp_path),
        environ=_halctf_env(**{FLAG_PATTERN_ENV: "   "}),
    )
    async with StateGraph(":memory:") as graph:
        await _seed_observed_text(graph, "leaked HALCTF{abc_123}")
        candidates = await environment.flag_extractor(run_id="run-1").extract(graph)

        assert [candidate.flag for candidate in candidates] == ["HALCTF{abc_123}"]


# ---------------------------------------------------------------------------
# Operator override (HAL-007)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_pattern_override_wins_over_halctf_default(
    tmp_path: Path,
) -> None:
    """An operator-set OZZGRAPH_FLAG_PATTERN replaces the HalCTF default:
    only the operator's CTF{...} shape persists — the HALCTF{...} flag
    string itself never becomes a candidate.

    Extraction is verbatim substring matching (the local default has the
    same semantics: ``flag{...}`` matches inside ``xflag{...}``), so the
    narrow operator pattern also matches the ``CTF{...}`` substring
    inside ``HALCTF{...}``; the assertions are on the persisted flag
    text, never on the HALCTF string.
    """
    operator_pattern = r"CTF\{[^{}\s]+\}"
    env = _halctf_env(
        HAL_USER_ID="user-42",
        **{FLAG_PATTERN_ENV: operator_pattern},
    )
    # Production path: load_config mirrors the env override into
    # config.flag_pattern; the environment prefers it over the default.
    config = load_config(environ=env)
    environment = HalCTFEnvironment(config, environ=env)
    async with StateGraph(":memory:") as graph:
        await _seed_observed_text(graph, "found HALCTF{abc_123} and CTF{winner}")
        candidates = await environment.flag_extractor(run_id="run-1").extract(graph)
        flags = [candidate.flag for candidate in candidates]

    assert config.flag_pattern == operator_pattern
    assert "HALCTF{abc_123}" not in flags
    assert all(re.fullmatch(operator_pattern, flag) for flag in flags)
    assert "CTF{winner}" in flags


# ---------------------------------------------------------------------------
# Local default unchanged (HAL-007)
# ---------------------------------------------------------------------------


def test_local_default_unchanged(tmp_path: Path) -> None:
    """The local default is byte-for-byte unchanged: the config default
    stays ``flag{...}`` and the HalCTF generalization lives only in the
    HalCTF environment."""
    assert DEFAULT_FLAG_PATTERN == r"flag\{[^{}\s]+\}"
    assert _config(tmp_path).flag_pattern == DEFAULT_FLAG_PATTERN
    assert HALCTF_DEFAULT_FLAG_PATTERN != DEFAULT_FLAG_PATTERN


@pytest.mark.asyncio
async def test_plain_extractor_default_still_flag_only() -> None:
    """A plain FlagCandidateExtractor (the local-mode default) matches
    only flag{...} — HALCTF{...} alone is never a candidate without the
    HalCTF environment."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_text(graph, "HALCTF{abc_123} but flag{abc} is real")
        candidates = await FlagCandidateExtractor().extract(graph)

        assert [candidate.flag for candidate in candidates] == ["flag{abc}"]
