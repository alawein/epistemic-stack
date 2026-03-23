"""Tests for shared/types.py — the domain model."""

from shared.types import (
    Claim, ClaimStatus, Evidence, EvidenceKind, ClaimEdge, EdgeRelation,
    ArchitecturalIntent, IntentSource, DriftViolation, Severity,
    ProofArtifact, VerificationStep,
)


class TestClaim:
    def test_defaults(self):
        c = Claim(statement="X uses Y")
        assert c.status == ClaimStatus.PROPOSED
        assert c.confidence == 0.5
        assert c.claim_type == "invariant"
        assert c.id  # UUID generated

    def test_is_active(self):
        c = Claim(status=ClaimStatus.VERIFIED)
        assert c.is_active()
        c.status = ClaimStatus.EXPIRED
        assert not c.is_active()

    def test_verification_level_unsupported(self):
        c = Claim()
        assert c.verification_level() == "unsupported"

    def test_verification_level_tested(self):
        c = Claim(evidence=[Evidence(kind=EvidenceKind.TEST_RESULT, description="pass")])
        assert c.verification_level() == "tested"

    def test_verification_level_proven(self):
        c = Claim(evidence=[Evidence(kind=EvidenceKind.FORMAL_PROOF, description="QED")])
        assert c.verification_level() == "proven"

    def test_verification_level_asserted(self):
        c = Claim(evidence=[Evidence(kind=EvidenceKind.HUMAN_ASSERTION, description="I checked")])
        assert c.verification_level() == "asserted"

    def test_verification_level_inferred(self):
        c = Claim(evidence=[Evidence(kind=EvidenceKind.LLM_REASONING, description="Looks right")])
        assert c.verification_level() == "inferred"

    def test_to_dict_roundtrip(self):
        c = Claim(statement="test", confidence=0.9)
        d = c.to_dict()
        assert d["statement"] == "test"
        assert d["confidence"] == 0.9
        assert d["status"] == "proposed"
        assert isinstance(d["evidence"], list)
        assert isinstance(d["scope"], dict)


class TestClaimEdge:
    def test_to_dict(self):
        e = ClaimEdge(source_id="a", target_id="b", relation=EdgeRelation.SUPPORTS)
        d = e.to_dict()
        assert d["relation"] == "supports"


class TestProofArtifact:
    def test_pass_rate_empty(self):
        p = ProofArtifact()
        assert p.pass_rate() == 0.0

    def test_pass_rate_ignores_rollbacks(self):
        p = ProofArtifact(steps=[
            VerificationStep(1, "check", "test", "ok", "ok", True),
            VerificationStep(2, "rollback", "rollback", "clean", "clean", True),
            VerificationStep(3, "check2", "test", "ok", "ok", False),
        ])
        # 2 testable steps (excluding rollback), 1 passed -> 50%
        assert p.pass_rate() == 0.5


class TestDriftViolation:
    def test_to_dict_severity(self):
        v = DriftViolation(file="a.ts", severity=Severity.CRITICAL)
        assert v.to_dict()["severity"] == "critical"


class TestArchitecturalIntent:
    def test_to_dict_source(self):
        i = ArchitecturalIntent(description="no X", source=IntentSource.ADR)
        assert i.to_dict()["source"] == "adr"
