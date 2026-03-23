"""Tests for claude-memory-mesh server — claim storage, queries, decay, edges."""

from unittest.mock import patch

import claude_memory_mesh_server as mms

from claude_memory_mesh_server import (
    store_claim, before_modifying, record_decision, record_failure,
    query_claims, invalidate_for_file, add_relationship, run_decay,
    memory_stats, _db, _meets_threshold,
)


class TestMeetsThreshold:
    def test_invariant_needs_evidence(self):
        assert _meets_threshold("invariant", 0.8, has_evidence=False, evidence_only_llm=False) is False

    def test_observation_low_bar(self):
        assert _meets_threshold("observation", 0.3, has_evidence=False, evidence_only_llm=False) is True

    def test_failure_needs_evidence(self):
        assert _meets_threshold("failure", 0.5, has_evidence=False, evidence_only_llm=False) is False

    def test_unknown_type_uses_default(self):
        assert _meets_threshold("custom_type", 0.5, has_evidence=False, evidence_only_llm=False) is True

    def test_llm_penalty_raises_bar(self):
        # constraint threshold is 0.6, +0.2 for LLM-only = 0.8 required
        assert _meets_threshold("constraint", 0.7, has_evidence=True, evidence_only_llm=True) is False
        assert _meets_threshold("constraint", 0.8, has_evidence=True, evidence_only_llm=True) is True


class TestStoreClaim:
    def test_stores_verified_claim(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = store_claim(
                "Auth uses JWT", claim_type="invariant", confidence=0.8,
                evidence_kind="test_result", evidence_description="test passed",
            )
            assert result["stored"] is True
            assert result["status"] == "verified"

    def test_rejects_below_threshold(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = store_claim(
                "Maybe X", claim_type="invariant", confidence=0.3,
            )
            assert result["stored"] is False
            assert "threshold" in result["reason"].lower()

    def test_force_stores_as_proposed(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = store_claim(
                "Uncertain claim", claim_type="invariant", confidence=0.3,
                force=True,
            )
            assert result["stored"] is True
            assert result["status"] == "proposed"

    def test_llm_only_evidence_raises_threshold(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            # invariant threshold is 0.7, +0.2 for LLM-only = 0.9
            result = store_claim(
                "LLM guess", claim_type="invariant", confidence=0.8,
                evidence_kind="llm_reasoning", evidence_description="looks right",
            )
            assert result["stored"] is False  # 0.8 < 0.9

    def test_decision_no_evidence_required(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = store_claim(
                "We chose X", claim_type="decision", confidence=0.6,
            )
            assert result["stored"] is True

    def test_verification_level_set(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim(
                "Tested thing", claim_type="invariant", confidence=0.8,
                evidence_kind="test_result", evidence_description="passed",
                scope_files=["auth.ts"],
            )
            result = query_claims(query="Tested thing")
            assert result["count"] == 1
            assert result["claims"][0]["verification_level"] == "tested"


class TestBeforeModifying:
    def test_returns_relevant_claims(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim(
                "Auth uses JWT", claim_type="invariant", confidence=0.8,
                evidence_kind="test_result", evidence_description="verified",
                scope_files=["src/auth/handler.ts"],
            )
            record_failure(
                "Async refactor", "Broke token refresh",
                scope_files=["src/auth/handler.ts"],
            )
            result = before_modifying("src/auth/handler.ts")
            assert result["total"] >= 2
            assert len(result["constraints"]) >= 1
            assert len(result["past_failures"]) >= 1


class TestRecordDecision:
    def test_stores_with_reasoning(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = record_decision(
                "Use PostgreSQL", "Need ACID transactions",
                alternatives_rejected=["MongoDB", "DynamoDB"],
            )
            assert result["stored"] is True
            claims = query_claims(query="PostgreSQL")
            assert claims["count"] == 1
            assert "DECISION:" in claims["claims"][0]["statement"]


class TestRecordFailure:
    def test_stores_failure(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = record_failure("Sync ORM in async", "Blocked event loop")
            assert result["stored"] is True
            claims = query_claims(claim_type="failure")
            assert claims["count"] >= 1


class TestQueryClaims:
    def test_filters_by_type(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("A", claim_type="invariant", confidence=0.8,
                        evidence_kind="test_result", evidence_description="ok")
            store_claim("B", claim_type="decision", confidence=0.6)
            result = query_claims(claim_type="decision")
            assert all(c["claim_type"] == "decision" for c in result["claims"])

    def test_filters_by_file(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("X about auth", claim_type="invariant", confidence=0.8,
                        evidence_kind="test_result", evidence_description="ok",
                        scope_files=["src/auth.ts"])
            store_claim("Y about pay", claim_type="invariant", confidence=0.8,
                        evidence_kind="test_result", evidence_description="ok",
                        scope_files=["src/pay.ts"])
            result = query_claims(scope_file="src/auth.ts")
            assert result["count"] == 1
            assert "auth" in result["claims"][0]["statement"]

    def test_excludes_expired(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("Fresh", claim_type="decision", confidence=0.6)
            # Manually expire a claim
            conn = _db(memory_db)
            conn.execute("INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at) VALUES ('old','Stale','observation',0.5,'expired',datetime('now'))")
            conn.commit()
            conn.close()
            result = query_claims(query="Stale")
            assert result["count"] == 0


class TestInvalidateForFile:
    def test_expires_file_bound_claims(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("Observation about auth", claim_type="observation",
                        confidence=0.5, scope_files=["src/auth.ts"],
                        evidence_kind="human_assertion", evidence_description="checked",
                        force=True)
            result = invalidate_for_file("src/auth.ts")
            assert result["expired"] >= 1

    def test_preserves_decisions(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            record_decision("Use JWT", "Security", scope_files=["src/auth.ts"])
            result = invalidate_for_file("src/auth.ts")
            assert result["preserved"] >= 1


class TestAddRelationship:
    def test_supports_edge(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            r1 = store_claim("A", claim_type="decision", confidence=0.6)
            r2 = store_claim("B", claim_type="decision", confidence=0.6)
            result = add_relationship(r1["claim_id"], r2["claim_id"], "supports")
            assert result["stored"] is True

    def test_supersedes_expires_target(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            r1 = store_claim("Old", claim_type="decision", confidence=0.6)
            r2 = store_claim("New", claim_type="decision", confidence=0.6)
            add_relationship(r2["claim_id"], r1["claim_id"], "supersedes")
            claims = query_claims(query="Old")
            assert claims["count"] == 0  # expired by supersedes

    def test_contradicts_marks_contested(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            r1 = store_claim("X is true", claim_type="invariant", confidence=0.8,
                             evidence_kind="test_result", evidence_description="ok")
            r2 = store_claim("X is false", claim_type="invariant", confidence=0.8,
                             evidence_kind="test_result", evidence_description="ok")
            add_relationship(r1["claim_id"], r2["claim_id"], "contradicts")
            # Both should now be contested — query via the server's _db to use same path
            conn = _db()
            row = conn.execute("SELECT status FROM claims WHERE id=?",
                               (r1["claim_id"],)).fetchone()
            conn.close()
            assert row["status"] == "contested"

    def test_invalid_relation_rejected(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = add_relationship("a", "b", "hates")
            assert "error" in result


class TestRunDecay:
    def test_expires_old_observations(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            from datetime import datetime, timezone, timedelta
            from uuid import uuid4
            old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            conn = _db()
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at) "
                "VALUES (?,?,'observation',0.5,'verified',?)",
                (str(uuid4()), "Old observation", old_ts),
            )
            conn.commit()
            conn.close()
            result = run_decay()
            assert result["expired"] >= 1


class TestMemoryStats:
    def test_returns_counts(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("A", claim_type="decision", confidence=0.6)
            store_claim("B", claim_type="invariant", confidence=0.8,
                        evidence_kind="test_result", evidence_description="ok")
            result = memory_stats()
            assert "by_status" in result
            assert "by_type" in result
            assert "recent" in result


# ── store_claim edge cases ────────────────────────────────────────────────

class TestStoreClaimEdgeCases:
    def test_verification_level_asserted(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("Human checked", claim_type="decision", confidence=0.6,
                        evidence_kind="human_assertion", evidence_description="I verified")
            result = query_claims(query="Human checked")
            assert result["claims"][0]["verification_level"] == "asserted"

    def test_verification_level_unsupported(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("No evidence", claim_type="decision", confidence=0.6)
            result = query_claims(query="No evidence")
            assert result["claims"][0]["verification_level"] == "unsupported"

    def test_invalid_evidence_kind_defaults_to_llm(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            # "made_up" is not a valid EvidenceKind, should default to llm_reasoning
            result = store_claim("Guess", claim_type="observation", confidence=0.5,
                                 evidence_kind="made_up", evidence_description="idk")
            assert result["stored"] is True


# ── run_decay edge cases ─────────────────────────────────────────────────

class TestRunDecayEdgeCases:
    def test_decisions_never_expire(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            from datetime import datetime, timezone, timedelta
            from uuid import uuid4
            old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
            conn = _db(memory_db)
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at) "
                "VALUES (?,?,'decision',0.9,'verified',?)",
                (str(uuid4()), "Old decision", old_ts),
            )
            conn.commit(); conn.close()
            result = run_decay()
            assert result["expired"] == 0

    def test_failures_expire_at_60_days(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            from datetime import datetime, timezone, timedelta
            from uuid import uuid4
            old_ts = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
            conn = _db(memory_db)
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at) "
                "VALUES (?,?,'failure',0.8,'verified',?)",
                (str(uuid4()), "Old failure", old_ts),
            )
            conn.commit(); conn.close()
            result = run_decay()
            assert result["expired"] >= 1

    def test_invariants_expire_at_90_days(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            from datetime import datetime, timezone, timedelta
            from uuid import uuid4
            old_ts = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
            conn = _db(memory_db)
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at,evidence) "
                "VALUES (?,?,'invariant',0.8,'verified',?,'[]')",
                (str(uuid4()), "Old invariant", old_ts),
            )
            conn.commit(); conn.close()
            result = run_decay()
            assert result["expired"] >= 1

    def test_decay_project_root_filter(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            from datetime import datetime, timezone, timedelta
            from uuid import uuid4
            old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            conn = _db(memory_db)
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at,project_root) "
                "VALUES (?,?,'observation',0.5,'verified',?,?)",
                (str(uuid4()), "Proj A obs", old_ts, "/proj-a"),
            )
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at,project_root) "
                "VALUES (?,?,'observation',0.5,'verified',?,?)",
                (str(uuid4()), "Proj B obs", old_ts, "/proj-b"),
            )
            conn.commit(); conn.close()
            result = run_decay(project_root="/proj-a")
            assert result["expired"] == 1  # only proj-a


# ── Other edge cases ─────────────────────────────────────────────────────

class TestMemoryMeshEdgeCases:
    def test_before_modifying_empty(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            result = before_modifying("nonexistent/file.ts")
            assert result["total"] == 0
            assert result["constraints"] == []
            assert result["past_failures"] == []

    def test_query_claims_with_limit(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            for i in range(5):
                store_claim(f"Claim {i}", claim_type="decision", confidence=0.6)
            result = query_claims(limit=2)
            assert result["count"] == 2

    def test_query_claims_project_root_filter(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            store_claim("In proj A", claim_type="decision", confidence=0.6,
                        project_root="/proj-a")
            store_claim("In proj B", claim_type="decision", confidence=0.6,
                        project_root="/proj-b")
            result = query_claims(project_root="/proj-a")
            assert result["count"] == 1
            assert "proj A" in result["claims"][0]["statement"]
