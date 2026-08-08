"""HalCTF environment package — adapter + HalCTF-owned services (V09).

The full HalCTF adapter (docs/CHANGES_v2.md milestone 9; docs/adr/0011).
This package is the ONLY HalCTF surface the generic kernel touches: the
environment adapter (``environment.py``) implements the
:class:`~ozzgraph.environments.base.EnvironmentAdapter` protocol, and
the hint / submission / flag / scoreboard modules that used to live in
the generic kernel (``ozzgraph.hints``, ``ozzgraph.submissions``,
``ozzgraph.flags`` — all deleted) now live HERE.

Kernel decoupling (V09): the generic kernel (supervisor, runner,
router, reducer, specialists, halctl) MUST NOT import
``ozzgraph.hints`` / ``ozzgraph.submissions`` / ``ozzgraph.flags``.
The supervisor reaches the moved services through THIS package
(``from ozzgraph.environments.halctf import HintCoordinator, ...``) or
through the environment's service factories; the shared generic entity
vocabulary (``observation`` / ``evidence``) lives in
:mod:`ozzgraph.entities`.
"""

from __future__ import annotations

from ozzgraph.entities import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    ENTITY_EVIDENCE,
    ENTITY_OBSERVATION,
)
from ozzgraph.environments.halctf.environment import (
    DEFAULT_HALCTF_CAPABILITIES,
    HALCTF_OBJECTIVE_DESCRIPTION,
    HALCTF_OBJECTIVE_ID,
    HALCTF_OBJECTIVE_SUCCESS_HINT,
    HalCTFEnvironment,
)
from ozzgraph.environments.halctf.flags import (
    EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
    ENTITY_FLAG_CANDIDATE,
    FIELD_ATTEMPTS,
    FIELD_EVIDENCE_IDS,
    FIELD_FLAG,
    FIELD_REJECTED,
    FIELD_SOURCE_OBSERVATION_ID,
    FIELD_VERIFIED,
    FLAGS_PRODUCER,
    FlagCandidate,
    FlagCandidateExtractor,
    FlagsError,
    FlagsStateError,
    InvalidFlagPatternError,
    flag_candidate_id,
)
from ozzgraph.environments.halctf.hints import (
    ENTITY_HINT_PURCHASE,
    ENTITY_HINT_RECOMMENDATION,
    EV_STALL_FLOOR,
    FREE_HINT_INDEX,
    HINTS_PRODUCER,
    INFORMATION_GAIN_ENTITY_TYPES,
    MIN_EV_GAIN,
    REQUIRED_RECOMMENDATIONS,
    RULE_BUDGET,
    RULE_FREE_HINT,
    RULE_LOW_COST_EXHAUSTED,
    RULE_NO_RECENT_INFORMATION_GAIN,
    RULE_SUFFICIENT_EV,
    RULE_TWO_RECOMMENDATIONS,
    HintClient,
    HintCoordinator,
    HintError,
    HintPolicy,
    HintPolicyDeniedError,
    HintPrivilegeError,
    HintStateError,
    PaidHintDecision,
    PaidHintRequest,
    hint_recommendation_id,
)
from ozzgraph.environments.halctf.scoreboard import (
    SCOREBOARD_PRODUCER,
    ScoreboardClient,
    ScoreboardCoordinator,
    ScoreboardError,
)
from ozzgraph.environments.halctf.submissions import (
    SUBMISSIONS_PRODUCER,
    SubmissionClient,
    SubmissionCoordinator,
    SubmissionError,
    SubmissionLimitError,
    SubmissionPrivilegeError,
    SubmissionRejectedError,
    SubmissionStateError,
)

__all__ = [
    "DEFAULT_HALCTF_CAPABILITIES",
    "EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION",
    "EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE",
    "ENTITY_EVIDENCE",
    "ENTITY_FLAG_CANDIDATE",
    "ENTITY_HINT_PURCHASE",
    "ENTITY_HINT_RECOMMENDATION",
    "ENTITY_OBSERVATION",
    "EV_STALL_FLOOR",
    "FIELD_ATTEMPTS",
    "FIELD_EVIDENCE_IDS",
    "FIELD_FLAG",
    "FIELD_REJECTED",
    "FIELD_SOURCE_OBSERVATION_ID",
    "FIELD_VERIFIED",
    "FLAGS_PRODUCER",
    "FREE_HINT_INDEX",
    "HALCTF_OBJECTIVE_DESCRIPTION",
    "HALCTF_OBJECTIVE_ID",
    "HALCTF_OBJECTIVE_SUCCESS_HINT",
    "HINTS_PRODUCER",
    "INFORMATION_GAIN_ENTITY_TYPES",
    "MIN_EV_GAIN",
    "REQUIRED_RECOMMENDATIONS",
    "RULE_BUDGET",
    "RULE_FREE_HINT",
    "RULE_LOW_COST_EXHAUSTED",
    "RULE_NO_RECENT_INFORMATION_GAIN",
    "RULE_SUFFICIENT_EV",
    "RULE_TWO_RECOMMENDATIONS",
    "SCOREBOARD_PRODUCER",
    "SUBMISSIONS_PRODUCER",
    "FlagCandidate",
    "FlagCandidateExtractor",
    "FlagsError",
    "FlagsStateError",
    "HalCTFEnvironment",
    "HintClient",
    "HintCoordinator",
    "HintError",
    "HintPolicy",
    "HintPolicyDeniedError",
    "HintPrivilegeError",
    "HintStateError",
    "InvalidFlagPatternError",
    "PaidHintDecision",
    "PaidHintRequest",
    "ScoreboardClient",
    "ScoreboardCoordinator",
    "ScoreboardError",
    "SubmissionClient",
    "SubmissionCoordinator",
    "SubmissionError",
    "SubmissionLimitError",
    "SubmissionPrivilegeError",
    "SubmissionRejectedError",
    "SubmissionStateError",
    "flag_candidate_id",
    "hint_recommendation_id",
]
