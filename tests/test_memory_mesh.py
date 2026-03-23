"""Tests for claude-memory-mesh server — claim storage, queries, decay, edges."""

from unittest.mock import patch

import claude_memory_mesh_server as mms

from claude_memory_mesh_server import (
    store_claim, before_modifying, record_decision, record_failure,
    query_claims, invalidate_for_file, add_relationship, run_decay,
    memory_stats, _db,
)


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
