"""Tests for claude-proof server — verification chains, checkpoints, rollback."""

import subprocess
from unittest.mock import patch, MagicMock

from claude_proof_server import (
    begin_modification, checkpoint, verify_step, rollback,
    finalize_proof, quick_verify, list_active_proofs, promote_claims,
    _proofs, _cp_count,
)
import claude_memory_mesh_server as mms


def _clear():
    _proofs.clear()
    _cp_count.clear()


class TestBeginModification:
    def test_creates_proof_chain(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc123")):
            result = begin_modification("Migrate auth to async", "Run auth tests after each step")
        assert "proof_id" in result
        assert result["intent"] == "Migrate auth to async"
        assert result["checkpoint"]["n"] == 1
        assert result["proof_id"] in _proofs

    def test_creates_baseline_commit(self):
        _clear()
        calls = []
        def fake_git(args, cwd="."):
            calls.append(args)
            return True, "abc123"
        with patch("claude_proof_server._git", side_effect=fake_git):
            begin_modification("Test", "Plan")
        # Should call git add, git commit, git rev-parse
        assert any("add" in c for c in calls)
        assert any("commit" in c for c in calls)


class TestCheckpoint:
    def test_increments_checkpoint_number(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc123")):
            r = begin_modification("Test", "Plan")
            pid = r["proof_id"]
            cp1 = checkpoint(pid, "First change")
            cp2 = checkpoint(pid, "Second change")
        assert cp1["checkpoint"]["n"] == 2
        assert cp2["checkpoint"]["n"] == 3

    def test_invalid_proof_id(self):
        _clear()
        result = checkpoint("nonexistent", "Oops")
        assert "error" in result

    def test_creates_git_tag(self):
        _clear()
        calls = []
        def fake_git(args, cwd="."):
            calls.append(args)
            return True, "abc123"
        with patch("claude_proof_server._git", side_effect=fake_git):
            r = begin_modification("Test", "Plan")
            checkpoint(r["proof_id"], "Tagged step")
        assert any("tag" in c for c in calls)


class TestVerifyStep:
    def test_auto_verification_pass(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            result = verify_step(
                r["proof_id"], "Check tests pass", "test",
                "All green", test_command="pytest",
            )
        assert result["passed"] is True
        assert result["pass_rate"] == 1.0

    def test_auto_verification_fail(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAILED")
            result = verify_step(
                r["proof_id"], "Check tests pass", "test",
                "All green", test_command="pytest",
            )
        assert result["passed"] is False
        assert result["last_checkpoint"] is not None  # hint to rollback

    def test_manual_verification(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        result = verify_step(
            r["proof_id"], "Manual code review", "review",
            "Looks correct", manual_result="Approved", manual_passed=True,
        )
        assert result["passed"] is True

    def test_timeout_handling(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
            result = verify_step(
                r["proof_id"], "Slow test", "test",
                "Should finish", test_command="sleep 999",
            )
        assert result["passed"] is False
        assert "Timed out" in result["outcome"]

    def test_requires_command_or_manual(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        result = verify_step(r["proof_id"], "Nothing", "test", "Expected")
        assert "error" in result

    def test_invalid_proof_id(self):
        _clear()
        result = verify_step("bad", "X", "test", "Y", test_command="echo hi")
        assert "error" in result


class TestRollback:
    def test_rolls_back_to_last_checkpoint(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc123")):
            r = begin_modification("Test", "Plan")
            checkpoint(r["proof_id"], "Good state")
            result = rollback(r["proof_id"])
        assert "rolled_back_to" in result

    def test_rolls_back_to_specific_checkpoint(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc123")):
            r = begin_modification("Test", "Plan")
            checkpoint(r["proof_id"], "CP2")
            checkpoint(r["proof_id"], "CP3")
            result = rollback(r["proof_id"], to_checkpoint=2)
        assert result["rolled_back_to"]["n"] == 2

    def test_invalid_checkpoint(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        result = rollback(r["proof_id"], to_checkpoint=99)
        assert "error" in result

    def test_no_proofs(self):
        _clear()
        result = rollback("nonexistent")
        assert "error" in result


class TestFinalizeProof:
    def test_verified_when_all_pass(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            verify_step(r["proof_id"], "Step 1", "test", "OK", test_command="echo ok")
            verify_step(r["proof_id"], "Step 2", "test", "OK", test_command="echo ok")
        result = finalize_proof(r["proof_id"])
        assert result["status"] == "verified"
        assert result["pass_rate"] == 1.0
        assert len(result["promotable_claims"]) == 2
        assert r["proof_id"] not in _proofs  # cleaned up

    def test_partial_when_mixed(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            verify_step(r["proof_id"], "Pass", "test", "OK", test_command="echo ok")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAIL")
            verify_step(r["proof_id"], "Fail", "test", "OK", test_command="false")
        result = finalize_proof(r["proof_id"])
        assert result["status"] == "partial"
        assert result["pass_rate"] == 0.5

    def test_failed_when_none_pass(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAIL")
            verify_step(r["proof_id"], "Fail", "test", "OK", test_command="false")
        result = finalize_proof(r["proof_id"])
        assert result["status"] == "failed"

    def test_unverified_when_no_steps(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        result = finalize_proof(r["proof_id"])
        assert result["status"] == "unverified"

    def test_promotable_claims_format(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            verify_step(r["proof_id"], "Auth test", "test", "OK",
                        test_command="pytest tests/auth")
        result = finalize_proof(r["proof_id"])
        claim = result["promotable_claims"][0]
        # Should be ready for store_claim()
        assert "statement" in claim
        assert "confidence" in claim
        assert "evidence_kind" in claim
        assert claim["confidence"] == 0.85  # test method gets 0.85

    def test_writes_artifact_file(self, tmp_path):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            r = begin_modification("Test", "Plan", str(tmp_path))
        result = finalize_proof(r["proof_id"], str(tmp_path), "proof.json")
        assert result["artifact_file"] == "proof.json"
        assert (tmp_path / "proof.json").exists()


class TestQuickVerify:
    def test_passing_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="all good", stderr="")
            result = quick_verify("Check syntax", "echo ok")
        assert result["passed"] is True
        assert result["evidence"]["kind"] == "test_result"

    def test_failing_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = quick_verify("Bad check", "false")
        assert result["passed"] is False


class TestListActiveProofs:
    def test_lists_proofs(self):
        _clear()
        with patch("claude_proof_server._git", return_value=(True, "abc")):
            begin_modification("Proof A", "Plan A")
            begin_modification("Proof B", "Plan B")
        result = list_active_proofs()
        assert len(result["proofs"]) == 2

    def test_empty_when_none(self):
        _clear()
        result = list_active_proofs()
        assert len(result["proofs"]) == 0


class TestPromoteClaims:
    def test_promotes_to_memory_mesh(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            claims = [
                {"statement": "Verified: auth tests pass", "claim_type": "invariant",
                 "confidence": 0.85, "evidence_kind": "test_result",
                 "evidence_description": "pytest auth/ passed",
                 "evidence_command": "pytest tests/auth"},
            ]
            result = promote_claims(claims)
            assert result["promoted"] == 1
            assert result["failed"] == 0

            # Verify claim is in memory mesh
            from claude_memory_mesh_server import query_claims
            found = query_claims(query="auth tests pass")
            assert found["count"] == 1

    def test_rejects_below_threshold(self, memory_db):
        with patch.object(mms, "DB_PATH", memory_db):
            claims = [
                {"statement": "Low confidence guess", "claim_type": "invariant",
                 "confidence": 0.3},
            ]
            result = promote_claims(claims)
            assert result["promoted"] == 0
            assert result["failed"] == 1

    def test_end_to_end_proof_to_memory(self, memory_db):
        """Full flow: begin -> verify -> finalize -> promote."""
        _clear()
        with patch.object(mms, "DB_PATH", memory_db):
            with patch("claude_proof_server._git", return_value=(True, "abc")):
                r = begin_modification("Migrate auth", "Run tests at each step")
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
                verify_step(r["proof_id"], "Auth tests pass", "test",
                            "All green", test_command="pytest tests/auth")
            result = finalize_proof(r["proof_id"])
            assert result["status"] == "verified"

            # Now promote
            promoted = promote_claims(result["promotable_claims"])
            assert promoted["promoted"] == 1

            # Verify claim landed in memory
            from claude_memory_mesh_server import query_claims
            found = query_claims(query="Auth tests pass")
            assert found["count"] == 1
            assert found["claims"][0]["verification_level"] == "tested"
