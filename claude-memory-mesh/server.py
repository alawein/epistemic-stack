#!/usr/bin/env python3
"""
claude-memory-mesh: Graph-Structured Project Memory

Claims need evidence to enter. Typed edges. Bi-temporal tracking. Automatic decay.
Not a chat log — a graph of justified, revisable beliefs.

  claude mcp add claude-memory-mesh -- python /path/to/claude-memory-mesh/server.py
"""

from __future__ import annotations
import json, os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastmcp import FastMCP
from shared.types import EvidenceKind, EdgeRelation

DB_PATH = os.environ.get("MEMORY_MESH_DB", str(Path.home() / ".claude" / "memory-mesh.db"))

def _now(): return datetime.now(timezone.utc).isoformat()

def _db(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS claims (
            id TEXT PRIMARY KEY, statement TEXT NOT NULL, claim_type TEXT DEFAULT 'invariant',
            confidence REAL DEFAULT 0.5, status TEXT DEFAULT 'proposed',
            evidence TEXT DEFAULT '[]', scope TEXT DEFAULT '{}', provenance TEXT DEFAULT '{}',
            observed_at TEXT NOT NULL, valid_until TEXT, tags TEXT DEFAULT '[]',
            project_root TEXT, verification_level TEXT DEFAULT 'unsupported',
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT REFERENCES claims(id) ON DELETE CASCADE,
            target_id TEXT REFERENCES claims(id) ON DELETE CASCADE,
            relation TEXT NOT NULL, weight REAL DEFAULT 1.0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source_id, target_id, relation)
        );
        CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
        CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project_root);
    """)
    conn.commit()
    return conn

# Verification thresholds: (min_confidence, requires_evidence)
THRESHOLDS = {
    "invariant": (0.7, True), "constraint": (0.6, True), "decision": (0.5, False),
    "observation": (0.3, False), "failure": (0.3, True),
}
# Decay: (max_age_days or None, invalidate_on_file_change)
DECAY = {
    "observation": (30, True), "invariant": (90, True), "decision": (None, False),
    "constraint": (None, True), "failure": (60, False),
}

def _meets_threshold(claim_type: str, confidence: float, has_evidence: bool, evidence_only_llm: bool) -> bool:
    min_conf, needs_ev = THRESHOLDS.get(claim_type, (0.5, False))
    if needs_ev and not has_evidence: return False
    if evidence_only_llm: min_conf += 0.2
    return confidence >= min_conf

mcp = FastMCP("claude-memory-mesh",
    instructions="Graph-structured project memory. Claims need evidence to enter. Typed edges. Automatic decay.")

@mcp.tool()
def store_claim(statement: str, claim_type: str = "invariant", confidence: float = 0.7,
                evidence_kind: Optional[str] = None, evidence_description: Optional[str] = None,
                evidence_command: Optional[str] = None, scope_files: Optional[list[str]] = None,
                tags: Optional[list[str]] = None, project_root: Optional[str] = None,
                agent_id: str = "claude-code", force: bool = False) -> dict:
    """Store a claim. Must meet verification threshold unless force=True (stored as 'proposed')."""
    ev_list = []
    if evidence_kind and evidence_description:
        kind = evidence_kind if evidence_kind in [e.value for e in EvidenceKind] else "llm_reasoning"
        ev_list.append({"kind": kind, "description": evidence_description,
                        "reproducible_command": evidence_command, "created_at": _now()})

    has_ev = len(ev_list) > 0
    only_llm = has_ev and all(e["kind"] == "llm_reasoning" for e in ev_list)
    verified = _meets_threshold(claim_type, confidence, has_ev, only_llm)

    if not verified and not force:
        min_c = THRESHOLDS.get(claim_type, (0.5, False))[0]
        return {"stored": False, "reason": "Below threshold",
                "hint": f"Need confidence >= {min_c} with evidence. Use force=True to store as proposed."}

    claim_id = str(__import__("uuid").uuid4())
    status = "verified" if verified else "proposed"
    conn = _db()
    conn.execute("""INSERT OR REPLACE INTO claims
        (id,statement,claim_type,confidence,status,evidence,scope,provenance,observed_at,tags,project_root,verification_level)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (claim_id, statement, claim_type, confidence, status,
         json.dumps(ev_list), json.dumps({"files": scope_files or []}),
         json.dumps({"agent_id": agent_id}), _now(), json.dumps(tags or []),
         project_root,
         "tested" if any(e["kind"] in ("test_result","static_analysis","ast_check") for e in ev_list)
         else "asserted" if any(e["kind"]=="human_assertion" for e in ev_list)
         else "inferred" if ev_list else "unsupported"))
    conn.commit(); conn.close()
    return {"stored": True, "claim_id": claim_id, "status": status, "confidence": confidence}


@mcp.tool()
def before_modifying(file_path: str, project_root: Optional[str] = None) -> dict:
    """Get all relevant claims BEFORE modifying a file. Constraints, past failures, decisions."""
    conn = _db()
    rows = conn.execute("""SELECT * FROM claims WHERE status IN ('verified','proposed','contested')
        AND (project_root=? OR project_root IS NULL) AND (scope LIKE ? OR tags LIKE ?)
        ORDER BY confidence DESC LIMIT 30""",
        (project_root, f"%{file_path}%", f"%{file_path}%")).fetchall()
    conn.close()
    claims = [dict(r) for r in rows]
    for c in claims:
        c["evidence"] = json.loads(c["evidence"])
        c["scope"] = json.loads(c["scope"])
        c["tags"] = json.loads(c["tags"])
    return {
        "file": file_path, "total": len(claims),
        "constraints": [c for c in claims if c["claim_type"] in ("invariant","constraint")],
        "decisions": [c for c in claims if c["claim_type"]=="decision"],
        "past_failures": [c for c in claims if c["claim_type"]=="failure"],
        "observations": [c for c in claims if c["claim_type"]=="observation"],
    }


@mcp.tool()
def record_decision(choice: str, reasoning: str, alternatives_rejected: Optional[list[str]] = None,
                    scope_files: Optional[list[str]] = None, tags: Optional[list[str]] = None,
                    project_root: Optional[str] = None) -> dict:
    """Record an architectural decision with reasoning. Doesn't decay by time."""
    return store_claim(f"DECISION: {choice}", "decision", 0.9, "human_assertion",
                       f"Reasoning: {reasoning}. Rejected: {', '.join(alternatives_rejected or ['none'])}",
                       scope_files=scope_files, tags=tags, project_root=project_root)


@mcp.tool()
def record_failure(what_failed: str, why: str, scope_files: Optional[list[str]] = None,
                   tags: Optional[list[str]] = None, project_root: Optional[str] = None) -> dict:
    """Record a failed approach so future sessions don't repeat it."""
    return store_claim(f"FAILED: {what_failed}. Reason: {why}", "failure", 0.85,
                       "llm_reasoning", f"Tried: {what_failed}. Failed: {why}",
                       scope_files=scope_files, tags=tags, project_root=project_root, force=True)


@mcp.tool()
def query_claims(query: Optional[str]=None, claim_type: Optional[str]=None,
                 scope_file: Optional[str]=None, project_root: Optional[str]=None, limit: int=20) -> dict:
    """Search the memory graph. Filter by text, type, file, project."""
    conn = _db()
    conds, params = ["status NOT IN ('expired','rejected')"], []
    if query: conds.append("statement LIKE ?"); params.append(f"%{query}%")
    if claim_type: conds.append("claim_type=?"); params.append(claim_type)
    if scope_file: conds.append("scope LIKE ?"); params.append(f"%{scope_file}%")
    if project_root: conds.append("(project_root=? OR project_root IS NULL)"); params.append(project_root)
    where = "WHERE " + " AND ".join(conds) if conds else ""
    rows = conn.execute(f"SELECT * FROM claims {where} ORDER BY confidence DESC LIMIT ?",
                        params + [limit]).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r); d["evidence"]=json.loads(d["evidence"]); d["scope"]=json.loads(d["scope"]); d["tags"]=json.loads(d["tags"])
        results.append(d)
    return {"count": len(results), "claims": results}


@mcp.tool()
def invalidate_for_file(file_path: str, project_root: Optional[str] = None) -> dict:
    """Expire claims tied to a changed file. Decisions are preserved."""
    conn = _db()
    invalidatable = {k for k, (_, inv) in DECAY.items() if inv}
    rows = conn.execute("SELECT id, claim_type, statement FROM claims WHERE status IN ('verified','proposed') AND scope LIKE ? AND (project_root=? OR project_root IS NULL)",
                        (f"%{file_path}%", project_root)).fetchall()
    expired = 0
    for r in rows:
        if r["claim_type"] in invalidatable:
            conn.execute("UPDATE claims SET status='expired', valid_until=?, updated_at=? WHERE id=?",
                         (_now(), _now(), r["id"]))
            expired += 1
    conn.commit(); conn.close()
    return {"file": file_path, "expired": expired, "preserved": len(rows)-expired}


@mcp.tool()
def add_relationship(source_id: str, target_id: str, relation: str) -> dict:
    """Add typed edge: supports | contradicts | supersedes | depends_on | derived_from."""
    if relation not in {r.value for r in EdgeRelation}:
        return {"error": f"Invalid. Use: {', '.join(r.value for r in EdgeRelation)}"}
    conn = _db()
    if relation == "supersedes":
        conn.execute("UPDATE claims SET status='expired', updated_at=? WHERE id=?", (_now(), target_id))
    if relation == "contradicts":
        conn.execute("UPDATE claims SET status='contested', updated_at=? WHERE id IN (?,?) AND status='verified'",
                     (_now(), source_id, target_id))
    conn.execute("INSERT OR IGNORE INTO edges (source_id,target_id,relation) VALUES (?,?,?)",
                 (source_id, target_id, relation))
    conn.commit(); conn.close()
    return {"stored": True, "relation": relation}


@mcp.tool()
def run_decay(project_root: Optional[str] = None) -> dict:
    """Expire stale claims based on age and confidence rules."""
    conn = _db()
    now = datetime.now(timezone.utc)
    expired = 0
    rows = conn.execute("SELECT * FROM claims WHERE status IN ('verified','proposed')").fetchall()
    for r in rows:
        if project_root and r["project_root"] and r["project_root"] != project_root: continue
        rule = DECAY.get(r["claim_type"])
        if not rule: continue
        max_days, _ = rule
        if max_days:
            observed = datetime.fromisoformat(r["observed_at"].replace("Z","+00:00"))
            if (now - observed).days > max_days:
                conn.execute("UPDATE claims SET status='expired', updated_at=? WHERE id=?", (_now(), r["id"]))
                expired += 1
    conn.commit(); conn.close()
    return {"expired": expired}


@mcp.tool()
def memory_stats(project_root: Optional[str] = None) -> dict:
    """Get counts by status, type, and recent activity."""
    conn = _db()
    by_status = {r["status"]:r["count"] for r in conn.execute("SELECT status, COUNT(*) as count FROM claims GROUP BY status").fetchall()}
    by_type = {r["claim_type"]:r["count"] for r in conn.execute("SELECT claim_type, COUNT(*) as count FROM claims WHERE status IN ('verified','proposed') GROUP BY claim_type").fetchall()}
    recent = [dict(r) for r in conn.execute("SELECT id,statement,status,confidence,updated_at FROM claims ORDER BY updated_at DESC LIMIT 5").fetchall()]
    conn.close()
    return {"by_status": by_status, "by_type": by_type, "recent": recent}


if __name__ == "__main__":
    mcp.run()
