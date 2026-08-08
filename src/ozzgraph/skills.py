"""Skill registry and initial skill packs for OzzGraph (PR17).

Implements the SKILL REGISTRY layer (docs/API_AND_INTEGRATIONS.md,
"Skill Registry"; PR step 17 of docs/IMPLEMENTATION_PLAN.md): a
deterministic registry of CTF skills with AGENTS.md rule #6 lazy
loading. :meth:`SkillRegistry.list_summaries` advertises compact
:class:`SkillSummary` cards per phase — what the context compiler
offers the model — and the full :class:`Skill` (the bounded skill
card, default timeout, and parser mappings) is fetched only when the
model selects a skill, via :meth:`SkillRegistry.load`.

Design rules (AGENTS.md):

- Deterministic: summaries are returned sorted by ``skill_id``, skill
  phases are deduplicated and ordered by the canonical
  :class:`~ozzgraph.phases.Phase` enum order, and the same registry
  state always yields the same summaries.
- The registry (:data:`SKILLS`) is a plain deterministic dict keyed by
  ``skill_id``, populated at import with the initial packs. It is
  explicitly not a plugin system (AGENTS.md rule #10): adding a skill
  means defining one :class:`Skill` and one :func:`register_skill`
  call — no discovery, no dynamic imports, no hidden global mutable
  state beyond the module-level registry constant.
- Failures are loud (AGENTS.md rule #9): an unknown ``skill_id``
  (:meth:`SkillRegistry.load`, :meth:`SkillRegistry.timeout_for`,
  :meth:`SkillRegistry.parsers_for`) and a skill mapping an
  unregistered ``(source, kind)`` parser key
  (:meth:`SkillRegistry.parsers_for`) both raise
  :class:`SkillRegistryError`. A broken registry entry is a
  configuration error and is never silently skipped.
- Kernel stays small (AGENTS.md rule #10): the registry only stores
  and resolves skills; nothing is wired into the supervisor, which
  PR18 (graph-driven phase router) owns.

Initial packs cover RECON, ENUMERATION, and EXPLOITATION. Every skill
card is bounded prompt text: purpose, bounded command guidance
consistent with the policy gate's command families, and an explicit
"Do NOT" list.
Parser mappings are consistent with the two built-in parsers
(``shell``/``text`` and ``halctl``/``json``, :mod:`ozzgraph.observations`).

V01 (docs/adr/0008): the FLAG_HUNT skill packs (filesystem hunt, web
artifact hunt, submission) were removed from the generic kernel with
the FLAG_HUNT / VERIFY_AND_SUBMIT phases; flag hunting and submission
are HalCTF environment behaviors arriving with the full adapter in V09.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ozzgraph.observations import Parser, ParserRegistryError, get_parser
from ozzgraph.phases import Phase


class SkillError(RuntimeError):
    """Base error for the skill layer (AGENTS.md rule #9)."""


class SkillRegistryError(SkillError):
    """Raised when the registry cannot resolve or accept a skill.

    Covers unknown ``skill_id`` lookups, duplicate registration, and
    skills whose parser mappings reference unregistered
    ``(source, kind)`` keys — all configuration errors that must fail
    loudly rather than silently degrade.
    """


class SkillSummary(BaseModel):
    """The compact advertisement for one skill (AGENTS.md rule #6).

    This is exactly what gets advertised to the model: skill id, short
    name, the phases the skill covers, and a one-line description.
    Deliberately bounded (``description`` carries a ``max_length``) and
    deliberately NOT carrying the skill card, timeout, or parser
    mappings — those are fetched lazily via
    :meth:`SkillRegistry.load` only after the model selects the skill.

    Instances are normally produced from a :class:`Skill` via
    :meth:`Skill.summary`, which guarantees the canonical phase order.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    phases: tuple[Phase, ...] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=200)


class Skill(BaseModel):
    """One registered CTF skill: the full card, loaded lazily.

    ``card`` is the bounded skill card content — prompt guidance,
    bounded command examples, and an explicit "what NOT to do" — that
    is fetched only when the model selects the skill (AGENTS.md rule
    #6). ``timeout_seconds`` is the skill's default action timeout.
    ``parsers`` maps each of the skill's output shapes to a registered
    ``(source, kind)`` parser key, resolved to live
    :class:`~ozzgraph.observations.Parser` instances by
    :meth:`SkillRegistry.parsers_for` against
    :data:`ozzgraph.observations.PARSERS`.
    ``required_capabilities`` (V03, docs/CHANGES_v2.md milestone 3) is
    the tool-plane contract: the first-class capability ids this skill
    needs from the environment (e.g. ``("http.request",)``) instead of
    binary names embedded in prompt text. :meth:`SkillRegistry.list_available`
    filters advertised skills against the capabilities an inventory
    actually provides.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    phases: tuple[Phase, ...] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=200)
    card: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    parsers: tuple[tuple[str, str], ...] = ()
    required_capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_phases(self) -> Self:
        """Deduplicate phases and order them by the Phase enum order.

        Deterministic rendering regardless of author input order: the
        canonical order is the :class:`Phase` definition order (the
        ARCHITECTURE.md phase order), and duplicates are dropped.
        """
        self.phases = tuple(phase for phase in Phase if phase in self.phases)
        self.required_capabilities = tuple(sorted(set(self.required_capabilities)))
        return self

    def summary(self) -> SkillSummary:
        """The compact advertisement for this skill.

        Derives the :class:`SkillSummary` from the skill's own fields
        (already normalized), so a summary can never drift from the
        skill it advertises.
        """
        return SkillSummary(
            skill_id=self.skill_id,
            name=self.name,
            phases=self.phases,
            description=self.description,
        )


class SkillRegistry:
    """Deterministic registry over a snapshot of :data:`SKILLS`.

    Lazy by design (AGENTS.md rule #6): :meth:`list_summaries` is the
    cheap advertisement path the context compiler uses per phase, and
    :meth:`load` fetches the full skill card only after the model
    selects a skill. Every method is deterministic — summaries are
    sorted by ``skill_id`` and never depend on dict iteration order.
    The registry is a plain wrapper: no discovery, no dynamic imports,
    and no hidden global mutable state (instances snapshot their
    mapping at construction; the module-level :data:`SKILLS` is never
    mutated by a registry instance).

    Args:
        skills: Optional mapping override (``skill_id`` -> Skill),
            defaulting to the module-level :data:`SKILLS`. Callers
            that need an isolated view pass their own mapping; it is
            copied, so later mutation of the caller's dict does not
            leak into the registry.
    """

    def __init__(self, skills: Mapping[str, Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = dict(SKILLS if skills is None else skills)

    def list_summaries(self, phase: Phase) -> list[SkillSummary]:
        """All skill summaries covering ``phase``, sorted by ``skill_id``.

        The advertisement path: compact, bounded, and cheap — no skill
        card, timeout, or parser data is materialized here.
        """
        return [
            skill.summary()
            for skill in sorted(self._skills.values(), key=lambda skill: skill.skill_id)
            if phase in skill.phases
        ]

    def list_available(self, capabilities: Collection[str]) -> list[SkillSummary]:
        """Summaries of skills whose requirements the capabilities satisfy.

        The V03 tool-contract bridge (docs/CHANGES_v2.md milestone 3):
        a skill is advertised only when EVERY capability it requires
        (``Skill.required_capabilities``) is provided by the given set
        (typically the inventory's available capabilities). A skill
        declaring no required capabilities is always available.
        Deterministic: sorted by ``skill_id``, never advertised when a
        required capability is missing.
        """
        provided = set(capabilities)
        return [
            skill.summary()
            for skill in sorted(self._skills.values(), key=lambda skill: skill.skill_id)
            if provided.issuperset(skill.required_capabilities)
        ]

    def load(self, skill_id: str) -> Skill:
        """The full skill card for ``skill_id`` (the lazy load step).

        Raises:
            SkillRegistryError: If ``skill_id`` is not registered.
        """
        try:
            return self._skills[skill_id]
        except KeyError:
            raise SkillRegistryError(f"no skill registered for id {skill_id!r}") from None

    def parsers_for(self, skill_id: str) -> list[Parser]:
        """Resolve the skill's parser mappings to live Parser instances.

        Resolves each ``(source, kind)`` pair declared by the skill
        against :data:`ozzgraph.observations.PARSERS`. A skill mapping
        an unregistered parser key is a broken registry entry and
        fails loudly (AGENTS.md rule #9) — it is never silently
        skipped or returned as an empty list.

        Raises:
            SkillRegistryError: If ``skill_id`` is not registered, or
                the skill maps an unregistered parser key.
        """
        skill = self.load(skill_id)
        parsers: list[Parser] = []
        for source, kind in skill.parsers:
            try:
                parsers.append(get_parser(source, kind))
            except ParserRegistryError as exc:
                raise SkillRegistryError(
                    f"skill {skill_id!r} maps unregistered parser source={source!r} kind={kind!r}"
                ) from exc
        return parsers

    def timeout_for(self, skill_id: str) -> int:
        """The skill's default action timeout in seconds.

        Raises:
            SkillRegistryError: If ``skill_id`` is not registered.
        """
        return self.load(skill_id).timeout_seconds


#: Deterministic registry: skill_id -> Skill. Populated at import with
#: the initial packs; extensible via :func:`register_skill` (explicit
#: registration only — no discovery, AGENTS.md rule #10).
SKILLS: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    """Register ``skill`` under its ``skill_id``.

    Raises:
        SkillRegistryError: If a skill is already registered for the
            id (duplicate registration fails loudly rather than
            silently overwriting).
    """
    if skill.skill_id in SKILLS:
        raise SkillRegistryError(f"a skill is already registered for id {skill.skill_id!r}")
    SKILLS[skill.skill_id] = skill


def _skill(
    *,
    skill_id: str,
    name: str,
    phases: Sequence[Phase],
    description: str,
    card: str,
    timeout_seconds: int,
    parsers: Sequence[tuple[str, str]] = (("shell", "text"),),
    required_capabilities: Sequence[str] = (),
) -> Skill:
    """Convenience constructor for initial-pack skills.

    Defaults to the ``shell``/``text`` parser mapping, the common case;
    halctl-facing skills (e.g. the submission skill) override it. The
    card text is embedded as data, never executed.
    """
    return Skill(
        skill_id=skill_id,
        name=name,
        phases=tuple(phases),
        description=description,
        card=card,
        timeout_seconds=timeout_seconds,
        parsers=tuple(parsers),
        required_capabilities=tuple(required_capabilities),
    )


# ---------------------------------------------------------------------------
# Initial packs: RECON
# ---------------------------------------------------------------------------

#: DNS enumeration: record lookups, zone-transfer attempts, and bounded
#: subdomain brute force.
RECON_DNS_ENUM = _skill(
    skill_id="recon_dns_enum",
    name="DNS enumeration",
    phases=(Phase.RECON,),
    description="DNS enumeration: record lookups, zone-transfer attempts, and bounded subdomain brute force",
    card=(
        "Purpose: map the target's DNS footprint before touching services.\n"
        "Commands (one bounded action each, never in a loop):\n"
        "- dig +short A <host> ; dig +short ANY <host>\n"
        "- dig AXFR @<ns> <zone> — zone transfer; success is a finding\n"
        "- host -t TXT <host> ; host -t MX <host>\n"
        '- ffuf -w <small wordlist> -u http://<host> -H "Host: FUZZ.<zone>"\n'
        "Record new names as hypotheses with the query as evidence.\n"
        "Do NOT: chain many lookups into one command, run huge wordlists in\n"
        "one action, or query addresses outside the target allowlist."
    ),
    timeout_seconds=60,
    required_capabilities=("dns.lookup", "web.content_discovery"),
)

#: HTTP service fingerprinting: headers, methods, TLS certificate, and
#: server tech stack.
RECON_HTTP_FINGERPRINT = _skill(
    skill_id="recon_http_fingerprint",
    name="HTTP service fingerprinting",
    phases=(Phase.RECON,),
    description="HTTP service fingerprinting: headers, methods, TLS certificate, and server tech stack",
    card=(
        "Purpose: identify the web server, framework, and exposed surface.\n"
        "Commands (bounded):\n"
        "- curl -sS -m 5 -I https://<target>/   (Server, X-Powered-By)\n"
        "- curl -sS -m 5 -o /dev/null -w '%{http_code} %{content_type}\\n' https://<target>/\n"
        "- curl -sS -m 5 -X OPTIONS -i https://<target>/   (allowed methods)\n"
        "- openssl s_client -connect <target>:443 -servername <target> </dev/null |\n"
        "  openssl x509 -noout -subject -issuer -dates\n"
        "Treat server/tech claims as hypotheses backed by the exact header.\n"
        "Do NOT: pull large bodies into context, follow redirects to other\n"
        "hosts, or scan ports from this skill (use recon_port_probe)."
    ),
    timeout_seconds=60,
    required_capabilities=("http.request", "network.tls_probe"),
)

#: Bounded TCP connect probes and banner grabs.
RECON_PORT_PROBE = _skill(
    skill_id="recon_port_probe",
    name="Bounded port and banner probing",
    phases=(Phase.RECON,),
    description="Bounded TCP connect probes and banner grabs to map reachable ports and services",
    card=(
        "Purpose: discover reachable TCP ports and their service banners.\n"
        "Commands (connect-based only, bounded):\n"
        "- nc -zvw 3 <host> <port-range>   (small ranges only, e.g. 1-1000)\n"
        "- nc -w 5 <host> <port> </dev/null   (banner grab, bounded)\n"
        "- ss -ltn ; netstat -ltn   (local listeners when access exists)\n"
        "Record each open port as a service hypothesis with the probe as\n"
        "evidence.\n"
        "Do NOT: SYN/stealth scans, sweep large ranges in one action, or\n"
        "paste banner content verbatim into context."
    ),
    timeout_seconds=90,
    required_capabilities=("network.probe", "network.listener"),
)


# ---------------------------------------------------------------------------
# Initial packs: ENUMERATION
# ---------------------------------------------------------------------------

#: Web content discovery: robots.txt, common paths, backups, bounded
#: wordlist fuzzing.
ENUM_WEB_CONTENT = _skill(
    skill_id="enum_web_content",
    name="Web content discovery",
    phases=(Phase.ENUMERATION,),
    description="Web content discovery: robots.txt, common paths, backups, and bounded wordlist fuzzing",
    card=(
        "Purpose: find hidden or forgotten web content.\n"
        "Commands (bounded):\n"
        "- curl -sS -m 5 https://<target>/robots.txt ; /sitemap.xml ; /.git/HEAD\n"
        "- ffuf -w <small wordlist> -mc 200,301,302,403 -u https://<target>/FUZZ\n"
        "- curl -sS -m 5 -o /dev/null -w '%{http_code}\\n' https://<target>/<candidate>\n"
        "Treat hits as hypotheses — a 200 page may still be a decoy.\n"
        "Do NOT: run huge wordlists in one action, follow every link\n"
        "recursively, or dump discovered files into model context."
    ),
    timeout_seconds=90,
    required_capabilities=("web.content_discovery", "http.request"),
)

#: Service version and banner extraction for known-vulnerability matching.
ENUM_SERVICE_VERSION = _skill(
    skill_id="enum_service_version",
    name="Service version extraction",
    phases=(Phase.ENUMERATION,),
    description="Service version and banner extraction to match software against known vulnerabilities",
    card=(
        "Purpose: pin exact service versions to known-vulnerability matches.\n"
        "Commands (bounded):\n"
        "- curl -sS -m 5 -I http://<target>/   (Server / X-Powered-By)\n"
        "- dig +short TXT <target> ; dig +short ANY <target>\n"
        "- nc -w 5 <host> <port> </dev/null   (plaintext banners)\n"
        "- searchsploit <product> <version>   (local exploit-db index)\n"
        "A version is a hypothesis until confirmed by a second independent\n"
        "source; record both observations.\n"
        "Do NOT: trust version strings from untrusted banners verbatim, run\n"
        "vulnerability scanners without a target hypothesis, or fetch remote\n"
        "CVE databases (no public internet)."
    ),
    timeout_seconds=60,
    # V10 (docs/BENCHMARKS.md, tool contract): version extraction needs
    # only HTTP/DNS/probe providers — the exploit-database search
    # (searchsploit) is a specialized deep-dive tool, NOT a requirement
    # for advertising this skill (it stays in the card as guidance and
    # in the catalog as a capability for Kali runtimes).
    required_capabilities=("http.request", "dns.lookup", "network.probe"),
)

#: HTTP application analysis: auth scheme, cookies, API endpoints.
ENUM_HTTP_APPLICATION = _skill(
    skill_id="enum_http_application",
    name="HTTP application analysis",
    phases=(Phase.ENUMERATION,),
    description="HTTP application analysis: auth scheme, cookies, API endpoints, and tech stack",
    card=(
        "Purpose: map the application's auth model and API surface.\n"
        "Commands (bounded):\n"
        "- curl -sS -m 5 -i https://<target>/   (cookies, WWW-Authenticate)\n"
        "- curl -sS -m 5 https://<target>/ -o <artifact>   (store, then grep)\n"
        "- grep -rhoE '(/api/[A-Za-z0-9_/-]+)' <artifact>   (endpoint harvest)\n"
        "- curl -sS -m 5 -i -X OPTIONS https://<target>/api/<endpoint>\n"
        "Record endpoints and the auth scheme as hypotheses with evidence.\n"
        "Do NOT: paste page bodies into context, crawl the whole site in one\n"
        "action, or attempt auth bypass here (that is exploitation)."
    ),
    timeout_seconds=90,
    required_capabilities=("http.request", "file.search"),
)


# ---------------------------------------------------------------------------
# Initial packs: EXPLOITATION
# ---------------------------------------------------------------------------

#: Bounded parameter tampering and injection probing.
EXPLOIT_PARAMETER_INJECTION = _skill(
    skill_id="exploit_parameter_injection",
    name="Parameter tampering and injection probing",
    phases=(Phase.EXPLOITATION,),
    description="Bounded parameter tampering and injection probing on identified request parameters",
    card=(
        "Purpose: probe identified parameters for injection classes.\n"
        "Commands (bounded, one parameter per action):\n"
        "- curl -sS -m 5 -i 'https://<target>/<path>?<param>=<probe>'\n"
        "- compare the probed response against the baseline response\n"
        "- sqlmap -u 'https://<target>/<path>?<param>=1' --batch --level 1\n"
        "  (only once a SQL-injection hypothesis is evidenced)\n"
        "Probe values are single tokens; record response deltas as evidence.\n"
        "Do NOT: destructive payloads (DROP, DELETE, rm) before a safe probe,\n"
        "batch many parameters at once, or retry identical probes."
    ),
    timeout_seconds=90,
    # V10 (docs/BENCHMARKS.md, tool contract): the skill's core is
    # bounded curl-based parameter probing — sqlmap is a specialized
    # follow-up, not a requirement for advertising this skill (the card
    # still guides toward it once an injection hypothesis is evidenced;
    # the capability stays in the catalog for Kali runtimes).
    required_capabilities=("http.request",),
)

#: Command injection detection with visible and blind markers.
EXPLOIT_COMMAND_INJECTION = _skill(
    skill_id="exploit_command_injection",
    name="Command injection detection",
    phases=(Phase.EXPLOITATION,),
    description="Command injection detection with visible and blind markers, non-destructive first",
    card=(
        "Purpose: detect command injection in parameters that feed commands.\n"
        "Commands (bounded, safe markers first):\n"
        "- visible: <param>=<value>;echo OZZG-MARK-<n>   (marker in response)\n"
        "- blind:   <param>=<value>;sleep 3   (timing delta only when allowed)\n"
        "- enumerate: <param>=<value>;id ; <param>=<value>;uname -a\n"
        "Use markers, never raw shell dumps; prove the sink before escalating.\n"
        "Do NOT: read secrets (e.g. /etc/shadow), write files on the target,\n"
        "connect to attacker infrastructure (no public internet), or run\n"
        "payloads that modify target state without a hypothesis."
    ),
    timeout_seconds=90,
    required_capabilities=("http.request",),
)

#: Authentication bypass checks: default credentials, cookie tampering,
#: path normalization.
EXPLOIT_AUTH_BYPASS = _skill(
    skill_id="exploit_auth_bypass",
    name="Authentication bypass checks",
    phases=(Phase.EXPLOITATION,),
    description="Authentication bypass checks: default credentials, cookie tampering, and path normalization",
    card=(
        "Purpose: test authentication and authorization boundaries.\n"
        "Commands (bounded):\n"
        "- default credentials: admin/admin, admin/password on the login endpoint\n"
        "- replay an altered session cookie against a protected path and\n"
        "  compare with the unauthenticated baseline\n"
        "- path normalization on protected routes: /admin, //admin, /./admin,\n"
        "  /%2e%2e/admin\n"
        "- JWT: base64-decode header/payload locally; test alg=none on a copy\n"
        "A bypass is a hypothesis until the protected resource differs from\n"
        "the baseline response.\n"
        "Do NOT: run large credential lists without explicit policy approval,\n"
        "target other users' sessions, or reuse altered credentials as facts."
    ),
    timeout_seconds=60,
    required_capabilities=("http.request", "crypto.decode"),
)

register_skill(RECON_DNS_ENUM)
register_skill(RECON_HTTP_FINGERPRINT)
register_skill(RECON_PORT_PROBE)
register_skill(ENUM_WEB_CONTENT)
register_skill(ENUM_SERVICE_VERSION)
register_skill(ENUM_HTTP_APPLICATION)
register_skill(EXPLOIT_PARAMETER_INJECTION)
register_skill(EXPLOIT_COMMAND_INJECTION)
register_skill(EXPLOIT_AUTH_BYPASS)
