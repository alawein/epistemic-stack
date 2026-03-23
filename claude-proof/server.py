#!/usr/bin/env python3
"""
claude-proof: Verified Code Modifications with Proof Chains

Every change declares intent + verification strategy.
Every step is checkpointed, verified, recorded.
Verified claims flow into claude-memory-mesh.

  claude mcp add claude-proof -- python /path/to/claude-proof/server.py
"""

from __future__ import annotations
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastmcp import FastMCP
from shared.types import ProofArtifact, VerificationStep, Evidence, EvidenceKind, Provenance

def _now(): return datetime.now(timezone.utc).isoformat()
def _git(args, cwd="."):
    try:
        r = subprocess.run(["git"]+args, capture_output=True, text=True, cwd=cwd, timeout=30)
        return r.returncode==0, r.stdout.strip()
    except Exception as e:
        return False, str(e)

_proofs: dict[str, ProofArtifact] = {}
_cp_count: dict[str, int] = {}

mcp = FastMCP("claude-proof",
    instructions="Verification chains for code modifications. Proof artifacts for auditable AI changes.")

@mcp.tool()
def begin_modification(intent: str, verification_plan: str, root: str = ".") -> dict:
    """Start a tracked modification. Declare intent and how you'll verify correctness."""
    proof = ProofArtifact(intent=intent, verification_plan=verification_plan, provenance=Provenance(agent_id="claude-code"))
    _git(["add","-A"], root)
    ok, sha = _git(["commit","--allow-empty","-m",f"[proof] baseline: {intent[:60]}"], root)
    if ok: _, sha = _git(["rev-parse","HEAD"], root)
    cp = {"n":1,"commit":sha,"desc":"Pre-modification baseline","at":_now()}
    proof.checkpoints.append(cp)
    _proofs[proof.id] = proof; _cp_count[proof.id] = 1
    return {"proof_id":proof.id,"intent":intent,"checkpoint":cp,
            "message":f"Proof chain started ({proof.id[:8]}). Make changes, then verify_step() and checkpoint()."}


@mcp.tool()
def checkpoint(proof_id: str, description: str, root: str = ".") -> dict:
    """Save current state. Rollback target if later steps fail."""
    proof = _proofs.get(proof_id)
    if not proof: return {"error":"No active proof chain with that ID."}
    n = _cp_count.get(proof_id,0)+1; _cp_count[proof_id] = n
    _git(["add","-A"], root)
    _git(["commit","--allow-empty","-m",f"[proof] cp{n}: {description[:60]}"], root)
    _, sha = _git(["rev-parse","HEAD"], root)
    _git(["tag",f"proof-{proof_id[:8]}-cp{n}",sha], root)
    cp = {"n":n,"commit":sha,"desc":description,"at":_now()}
    proof.checkpoints.append(cp)
    return {"proof_id":proof_id,"checkpoint":cp,"total_steps":len(proof.steps)}


@mcp.tool()
def verify_step(proof_id: str, description: str, method: str, expected: str,
                test_command: Optional[str]=None, manual_result: Optional[str]=None,
                manual_passed: Optional[bool]=None, root: str=".") -> dict:
    """Run a verification and record the result. Provide test_command (auto) or manual_result+manual_passed."""
    proof = _proofs.get(proof_id)
    if not proof: return {"error":"No active proof chain."}
    step = VerificationStep(step_number=len(proof.steps)+1, description=description,
                            method=method, expected_outcome=expected)
    if test_command:
        try:
            r = subprocess.run(test_command, shell=True, capture_output=True, text=True, cwd=root, timeout=120)
            step.passed = r.returncode==0
            step.actual_outcome = (r.stdout+"\n"+r.stderr).strip()[:2000]
        except subprocess.TimeoutExpired:
            step.passed, step.actual_outcome = False, "Timed out (120s)"
        step.evidence = Evidence(kind=EvidenceKind.TEST_RESULT if method=="test" else EvidenceKind.STATIC_ANALYSIS,
                                 description=f"cmd: {test_command}", reproducible_command=test_command)
    elif manual_result is not None:
        step.passed, step.actual_outcome = manual_passed, manual_result
        step.evidence = Evidence(kind=EvidenceKind.HUMAN_ASSERTION, description=manual_result)
    else:
        return {"error":"Provide test_command or manual_result+manual_passed."}
    proof.steps.append(step)
    return {"step":step.step_number,"passed":step.passed,"pass_rate":proof.pass_rate(),
            "outcome":step.actual_outcome[:500] if step.actual_outcome else None,
            "last_checkpoint": proof.checkpoints[-1] if not step.passed and proof.checkpoints else None}


@mcp.tool()
def rollback(proof_id: str, to_checkpoint: Optional[int]=None, root: str=".") -> dict:
    """Revert to a checkpoint. Default: most recent."""
    proof = _proofs.get(proof_id)
    if not proof or not proof.checkpoints: return {"error":"No checkpoints."}
    target = next((c for c in proof.checkpoints if c["n"]==to_checkpoint), None) if to_checkpoint else proof.checkpoints[-1]
    if not target: return {"error":f"Checkpoint {to_checkpoint} not found."}
    ok, out = _git(["reset","--hard",target["commit"]], root)
    if not ok: return {"error":f"Rollback failed: {out}"}
    proof.steps.append(VerificationStep(len(proof.steps)+1, f"Rollback to cp{target['n']}",
                                        "rollback", "Clean state", f"Reset to {target['commit'][:8]}", True))
    return {"rolled_back_to":target}


@mcp.tool()
def finalize_proof(proof_id: str, root: str=".", output_file: Optional[str]=None) -> dict:
    """Finalize proof chain. Produces artifact + claims ready for memory mesh promotion."""
    proof = _proofs.get(proof_id)
    if not proof: return {"error":"No active proof chain."}
    testable = [s for s in proof.steps if s.method != "rollback"]
    if not testable: proof.final_status = "unverified"
    elif all(s.passed for s in testable): proof.final_status = "verified"
    elif any(s.passed for s in testable): proof.final_status = "partial"
    else: proof.final_status = "failed"

    promotable = [{"statement":f"Verified: {s.description}","claim_type":"invariant",
                    "confidence":0.85 if s.method in ("test","static_analysis") else 0.6,
                    "evidence_kind":s.evidence.kind.value if s.evidence else "llm_reasoning",
                    "evidence_description":s.evidence.description if s.evidence else s.description,
                    "evidence_command":s.evidence.reproducible_command if s.evidence else None}
                   for s in testable if s.passed]

    artifact = proof.to_dict(); artifact["promotable_claims"] = promotable
    if output_file: (Path(root)/output_file).write_text(json.dumps(artifact, indent=2))
    del _proofs[proof_id]
    return {"proof_id":proof_id,"status":proof.final_status,"pass_rate":proof.pass_rate(),
            "steps":len(proof.steps),"promotable_claims":promotable,
            "artifact_file":output_file}


@mcp.tool()
def quick_verify(description: str, test_command: str, root: str=".") -> dict:
    """One-off verification without a full proof chain. Returns evidence for store_claim()."""
    try:
        r = subprocess.run(test_command, shell=True, capture_output=True, text=True, cwd=root, timeout=120)
        passed = r.returncode==0; output = (r.stdout+"\n"+r.stderr).strip()[:2000]
    except: passed, output = False, "Error running command"
    return {"passed":passed,"description":description,"command":test_command,"output":output[:500],
            "evidence":{"kind":"test_result","description":description,"reproducible_command":test_command}}


@mcp.tool()
def promote_claims(claims: list[dict], scope_files: Optional[list[str]] = None,
                   project_root: Optional[str] = None) -> dict:
    """Promote verified claims into memory mesh. Accepts the promotable_claims list from finalize_proof().

    Bridges claude-proof -> claude-memory-mesh: each claim is stored with its evidence.
    """
    # Reuse already-loaded memory mesh module if available, otherwise load fresh
    mesh = sys.modules.get("claude_memory_mesh_server")
    if not mesh:
        try:
            from importlib.util import spec_from_file_location, module_from_spec
            mesh_path = Path(__file__).resolve().parent.parent / "claude-memory-mesh" / "server.py"
            spec = spec_from_file_location("claude_memory_mesh_server", str(mesh_path))
            mesh = module_from_spec(spec)
            sys.modules["claude_memory_mesh_server"] = mesh
            spec.loader.exec_module(mesh)
        except Exception as e:
            return {"error": f"Cannot load claude-memory-mesh: {e}",
                    "hint": "Ensure claude-memory-mesh/server.py exists alongside claude-proof/"}

    promoted, failed = [], []
    for c in claims:
        result = mesh.store_claim(
            statement=c.get("statement", ""),
            claim_type=c.get("claim_type", "invariant"),
            confidence=c.get("confidence", 0.7),
            evidence_kind=c.get("evidence_kind"),
            evidence_description=c.get("evidence_description"),
            evidence_command=c.get("evidence_command"),
            scope_files=scope_files,
            project_root=project_root,
        )
        if result.get("stored"):
            promoted.append(result)
        else:
            failed.append({"claim": c["statement"], "reason": result.get("reason", "unknown")})

    return {"promoted": len(promoted), "failed": len(failed),
            "details": promoted, "failures": failed}


@mcp.tool()
def list_active_proofs() -> dict:
    """List in-progress proof chains."""
    return {"proofs":[{"id":p.id,"intent":p.intent,"steps":len(p.steps),
                       "checkpoints":len(p.checkpoints),"pass_rate":p.pass_rate()}
                      for p in _proofs.values()]}


if __name__ == "__main__":
    mcp.run()
