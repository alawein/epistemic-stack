"""
Epistemic primitives: Claims, Evidence, Scope, Provenance.

A Claim is NOT a fact — it's a justified, revisable belief about the codebase.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4


class ClaimStatus(str, Enum):
    PROPOSED  = "proposed"
    VERIFIED  = "verified"
    CONTESTED = "contested"
    EXPIRED   = "expired"
    REJECTED  = "rejected"


class EdgeRelation(str, Enum):
    SUPPORTS     = "supports"
    CONTRADICTS  = "contradicts"
    SUPERSEDES   = "supersedes"
    DEPENDS_ON   = "depends_on"
    DERIVED_FROM = "derived_from"


class EvidenceKind(str, Enum):
    TEST_RESULT     = "test_result"
    STATIC_ANALYSIS = "static_analysis"
    AST_CHECK       = "ast_check"
    IMPORT_TRACE    = "import_trace"
    GIT_DIFF        = "git_diff"
    HUMAN_ASSERTION = "human_assertion"
    RUNTIME_TRACE   = "runtime_trace"
    FORMAL_PROOF    = "formal_proof"
    LLM_REASONING   = "llm_reasoning"


class IntentSource(str, Enum):
    CLAUDE_MD        = "claude_md"
    ADR              = "adr"
    DRIFT_RULES      = "drift_rules"
    HUMAN_DECLARATION = "human"
    INFERRED         = "inferred"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Evidence:
    kind: EvidenceKind
    description: str
    data: dict = field(default_factory=dict)
    reproducible_command: Optional[str] = None
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class Scope:
    files: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Provenance:
    agent_id: str = "human"
    model: Optional[str] = None
    context_tokens: Optional[int] = None
    commit_at_creation: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Claim:
    id: str = field(default_factory=lambda: str(uuid4()))
    statement: str = ""
    claim_type: str = "invariant"  # invariant | decision | constraint | observation | failure
    confidence: float = 0.5
    status: ClaimStatus = ClaimStatus.PROPOSED
    evidence: list[Evidence] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    provenance: Provenance = field(default_factory=Provenance)
    observed_at: str = field(default_factory=_now)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "statement": self.statement,
            "claim_type": self.claim_type, "confidence": self.confidence,
            "status": self.status.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "scope": self.scope.to_dict(),
            "provenance": self.provenance.to_dict(),
            "observed_at": self.observed_at,
            "valid_from": self.valid_from, "valid_until": self.valid_until,
            "tags": self.tags,
        }

    def is_active(self) -> bool:
        return self.status in (ClaimStatus.PROPOSED, ClaimStatus.VERIFIED)

    def verification_level(self) -> str:
        if not self.evidence:
            return "unsupported"
        kinds = {e.kind for e in self.evidence}
        if EvidenceKind.FORMAL_PROOF in kinds:
            return "proven"
        if kinds & {EvidenceKind.TEST_RESULT, EvidenceKind.STATIC_ANALYSIS, EvidenceKind.AST_CHECK}:
            return "tested"
        if EvidenceKind.HUMAN_ASSERTION in kinds:
            return "asserted"
        return "inferred"


@dataclass
class ClaimEdge:
    source_id: str
    target_id: str
    relation: EdgeRelation
    weight: float = 1.0
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["relation"] = self.relation.value
        return d


@dataclass
class ArchitecturalIntent:
    id: str = field(default_factory=lambda: str(uuid4()))
    description: str = ""
    source: IntentSource = IntentSource.HUMAN_DECLARATION
    source_file: Optional[str] = None
    rule_type: Optional[str] = None  # import_boundary | layer_enforcement | prohibition
    rule_config: dict = field(default_factory=dict)
    confirmed: bool = False
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        return d


@dataclass
class DriftViolation:
    id: str = field(default_factory=lambda: str(uuid4()))
    intent_id: str = ""
    intent_description: str = ""
    file: str = ""
    line: Optional[int] = None
    evidence_text: str = ""
    confidence: float = 0.5
    severity: Severity = Severity.MEDIUM
    suggested_fix: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class VerificationStep:
    step_number: int
    description: str
    method: str
    expected_outcome: str
    actual_outcome: Optional[str] = None
    passed: Optional[bool] = None
    evidence: Optional[Evidence] = None
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.evidence:
            d["evidence"] = self.evidence.to_dict()
        return d


@dataclass
class ProofArtifact:
    id: str = field(default_factory=lambda: str(uuid4()))
    intent: str = ""
    verification_plan: str = ""
    steps: list[VerificationStep] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)
    final_status: str = "in_progress"
    claims_generated: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "intent": self.intent,
            "verification_plan": self.verification_plan,
            "steps": [s.to_dict() for s in self.steps],
            "checkpoints": self.checkpoints,
            "final_status": self.final_status,
            "claims_generated": self.claims_generated,
            "provenance": self.provenance.to_dict(),
            "created_at": self.created_at,
        }

    def pass_rate(self) -> float:
        testable = [s for s in self.steps if s.method != "rollback"]
        if not testable:
            return 0.0
        return sum(1 for s in testable if s.passed) / len(testable)
