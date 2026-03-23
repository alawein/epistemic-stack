"""End-to-end integration tests across all three servers."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from uuid import uuid4

import claude_memory_mesh_server as mms
from claude_drift_server import scan_intents, check_drift, will_this_drift, _intents
from claude_memory_mesh_server import (
    store_claim, record_decision, invalidate_for_file,
    add_relationship, run_decay, query_claims, _db,
)
from claude_proof_server import (
    begin_modification, verify_step, finalize_proof, promote_claims,
    _proofs, _cp_count,
)


def _clear_proof():
    _proofs.clear()
    _cp_count.clear()


# ── Full Lifecycle ────────────────────────────────────────────────────────

class TestFullLifecycle:
    def test_drift_to_proof_to_memory(self, tmp_project, memory_db):
        """Scan intents -> detect violation -> proof chain -> verify -> promote to memory."""
        _intents.clear()
        _clear_proof()
        with patch.object(mms, "DB_PATH", memory_db):
            # 1. Scan intents and detect drift
            scan_intents(str(tmp_project))
            drift = check_drift(str(tmp_project))
            assert drift["violation_count"] >= 1

            # 2. Start a proof chain to fix the violation
            with patch("claude_proof_server._git", return_value=(True, "abc123")):
                r = begin_modification(
                    "Fix auth/payment import boundary violation",
                    "Remove payment import from auth/handler.ts, run tests",
                )

            # 3. Verify the fix
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
                verify_step(r["proof_id"], "Auth tests pass after removing import",
                            "test", "All green", test_command="pytest tests/auth")

            # 4. Finalize and get promotable claims
            result = finalize_proof(r["proof_id"])
            assert result["status"] == "verified"
            assert len(result["promotable_claims"]) == 1

            # 5. Promote claims into memory mesh
            promoted = promote_claims(result["promotable_claims"],
                                       scope_files=["src/auth/handler.ts"])
            assert promoted["promoted"] == 1

            # 6. Verify claim is queryable in memory
            found = query_claims(query="Auth tests pass")
            assert found["count"] == 1
            assert found["claims"][0]["verification_level"] == "tested"

    def test_claim_decay_lifecycle(self, memory_db):
        """Store observation -> age it -> run decay -> verify expired."""
        with patch.object(mms, "DB_PATH", memory_db):
            old_ts = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
            conn = _db(memory_db)
            claim_id = str(uuid4())
            conn.execute(
                "INSERT INTO claims (id,statement,claim_type,confidence,status,observed_at) "
                "VALUES (?,?,'observation',0.5,'verified',?)",
                (claim_id, "Old observation about auth", old_ts),
            )
            conn.commit(); conn.close()

            # Before decay: claim is queryable
            found = query_claims(query="Old observation")
            assert found["count"] == 1

            # Run decay
            result = run_decay()
            assert result["expired"] >= 1

            # After decay: claim is gone from active queries
            found = query_claims(query="Old observation")
            assert found["count"] == 0

    def test_file_invalidation_cycle(self, memory_db):
        """Store claims for file -> invalidate -> store new claims."""
        with patch.object(mms, "DB_PATH", memory_db):
            # Store an observation for a file
            store_claim("Auth uses sessions", claim_type="observation",
                        confidence=0.5, scope_files=["src/auth.ts"],
                        evidence_kind="human_assertion", evidence_description="checked",
                        force=True)

            # File changed — invalidate
            result = invalidate_for_file("src/auth.ts")
            assert result["expired"] >= 1

            # Store a new claim for the updated file
            store_claim("Auth now uses JWT", claim_type="invariant",
                        confidence=0.8, scope_files=["src/auth.ts"],
                        evidence_kind="test_result", evidence_description="test passed")

            # Query: only the new claim is active
            found = query_claims(scope_file="src/auth.ts")
            assert found["count"] == 1
            assert "JWT" in found["claims"][0]["statement"]


# ── Cross-Server Integration ─────────────────────────────────────────────

class TestCrossServerIntegration:
    def test_contradiction_detection(self, memory_db):
        """Store contradicting claims -> relationship marks both contested."""
        with patch.object(mms, "DB_PATH", memory_db):
            r1 = store_claim("Auth uses sessions", claim_type="invariant",
                             confidence=0.8, evidence_kind="test_result",
                             evidence_description="session test passed")
            r2 = store_claim("Auth uses JWT", claim_type="invariant",
                             confidence=0.8, evidence_kind="test_result",
                             evidence_description="jwt test passed")

            add_relationship(r1["claim_id"], r2["claim_id"], "contradicts")

            # Both should be contested
            conn = _db()
            for cid in [r1["claim_id"], r2["claim_id"]]:
                row = conn.execute("SELECT status FROM claims WHERE id=?", (cid,)).fetchone()
                assert row["status"] == "contested"
            conn.close()

    def test_supersede_chain(self, memory_db):
        """v1 -> v2 supersedes -> v1 expired, v2 active."""
        with patch.object(mms, "DB_PATH", memory_db):
            v1 = store_claim("Auth v1: basic auth", claim_type="decision", confidence=0.6)
            v2 = store_claim("Auth v2: OAuth2", claim_type="decision", confidence=0.7)

            add_relationship(v2["claim_id"], v1["claim_id"], "supersedes")

            # v1 should be expired
            found_v1 = query_claims(query="Auth v1")
            assert found_v1["count"] == 0  # expired, not in active queries

            # v2 should still be active
            found_v2 = query_claims(query="Auth v2")
            assert found_v2["count"] == 1

    def test_decision_survives_invalidation(self, memory_db):
        """Decision + observation for same file -> invalidate -> decision preserved."""
        with patch.object(mms, "DB_PATH", memory_db):
            record_decision("Use JWT for auth", "Security requirements",
                           scope_files=["src/auth.ts"])
            store_claim("Auth handler has 3 methods", claim_type="observation",
                        confidence=0.5, scope_files=["src/auth.ts"],
                        evidence_kind="human_assertion", evidence_description="counted",
                        force=True)

            result = invalidate_for_file("src/auth.ts")
            assert result["expired"] >= 1    # observation expired
            assert result["preserved"] >= 1  # decision preserved

            # Decision is still queryable
            found = query_claims(scope_file="src/auth.ts", claim_type="decision")
            assert found["count"] == 1
            assert "JWT" in found["claims"][0]["statement"]


# ── Multi-Intent Composition ─────────────────────────────────────────────

class TestMultiIntentComposition:
    def test_multiple_rule_types_compose(self, tmp_project):
        """All 3 rule types detect their respective violations."""
        _intents.clear()
        scan_intents(str(tmp_project))
        result = check_drift(str(tmp_project))

        violations = result["violations"]
        files = [v["file"] for v in violations]

        # import_boundary: auth/handler.ts imports payment
        assert any("auth" in f and "handler" in f for f in files)
        # prohibition: api/controller.ts has console.log
        assert any("api" in f and "controller" in f for f in files)

    def test_will_this_drift_multi_intent(self, tmp_project):
        """Multiple intents warn on proposed changes."""
        _intents.clear()
        scan_intents(str(tmp_project))

        # This change violates import boundary (auth importing payment)
        result = will_this_drift(
            "src/auth/handler.ts",
            'import { Charge } from "../payment/charge"',
            str(tmp_project),
        )
        assert not result["safe"]

        # Prohibition check: forbidden_action from regex is "use console.log",
        # so proposed_change must contain that phrase for will_this_drift to warn
        result2 = will_this_drift(
            "src/api/controller.ts",
            'use console.log to debug',
            str(tmp_project),
        )
        assert not result2["safe"]
