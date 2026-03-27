---
type: canonical
source: none
sync: none
sla: none
---

# Epistemic Stack

Three MCP servers that give AI coding agents **justified, revisable beliefs** about your codebase.

```
claude-drift        detects divergence between intent and implementation
claude-memory-mesh  persists what agents believe (with evidence and decay)
claude-proof        verifies changes and gates what enters memory
```

Without proof, memory is noisy. Without memory, proof is local and wasted. Without drift detection, neither stays aligned with reality.

## The Problem

AI coding tools are stateless by session, blind to architectural intent, and unable to distinguish a tested invariant from a hallucinated guess. This creates:

1. **Repeated reasoning** -- agents rediscover the same constraint every session
2. **Contradictory decisions** -- isolated agents form incompatible models of the same code
3. **Invisible architectural drift** -- code diverges from intent and nobody measures it

Current memory solutions (CLAUDE.md, Cursor rules) are flat files with no relationships, no evidence, no scoped validity, no contradiction detection, and no decay.

## Quick Start

```bash
pip install fastmcp

# Add to Claude Code (adjust paths)
claude mcp add claude-drift -- python /path/to/claude-drift/server.py
claude mcp add claude-memory-mesh -- python /path/to/claude-memory-mesh/server.py
claude mcp add claude-proof -- python /path/to/claude-proof/server.py
```

## How It Works

### Layer 1: claude-drift (Detection)

Reads architectural intent from CLAUDE.md, ADRs, or `.drift-rules.json`. Derives enforceable rules from natural language. Scans code for violations. Produces a drift score.

**Tools:** `scan_intents`, `check_drift`, `will_this_drift`, `check_drift_for_changes`, `declare_intent`, `export_rules`

```
Your CLAUDE.md says "Auth should not import from payment."
claude-drift finds `import { PaymentSession } from '../payment/session'`
on line 47 of auth/handler.ts and flags it at 0.91 confidence.
```

### Layer 2: claude-memory-mesh (Persistence)

Graph-structured memory where claims need evidence to enter. Typed edges (supports / contradicts / supersedes). Bi-temporal tracking. Automatic decay by claim type.

**Tools:** `store_claim`, `before_modifying`, `record_decision`, `record_failure`, `query_claims`, `invalidate_for_file`, `add_relationship`, `run_decay`, `memory_stats`

```
Call before_modifying("auth/handler.ts").
Returns: 2 constraints, 1 past failure ("async refactor broke token refresh"),
1 decision ("chose JWT over sessions for cross-service auth").
```

### Layer 3: claude-proof (Commitment)

Wraps code modifications in verification chains. Every edit declares intent, gets checkpointed, tested, and recorded. Verified claims flow into memory mesh.

**Tools:** `begin_modification`, `checkpoint`, `verify_step`, `rollback`, `finalize_proof`, `quick_verify`, `list_active_proofs`

```
Start a proof chain for migrating auth to async.
Run tests at each step. If step 3 fails, rollback to checkpoint 2.
finalize_proof() outputs claims ready for store_claim().
```

## Composition

```
Code Change --> claude-proof verifies it --> verified claims promoted to claude-memory-mesh
                                                         |
              claude-drift checks <-- intents from CLAUDE.md + memory graph
```

The graph grows. Claims get evidence. Evidence gets verified. Verified claims inform future decisions. Stale claims decay. Contradictions surface.

## Marking Intents

Add `<!-- drift:intent -->` blocks to your CLAUDE.md:

```markdown
<!-- drift:intent -->
- Auth module should not import from payment domain
- All database access must go through the repository layer
- Never use console.log in src/production
<!-- /drift:intent -->
```

Or use `.drift-rules.json` for structured rules:

```json
{
  "version": "1.0",
  "rules": [
    {
      "id": "rule-001",
      "description": "Auth must not import payment",
      "rule_type": "import_boundary",
      "config": { "source_pattern": "src/auth", "forbidden_target": "src/payment" }
    }
  ]
}
```

claude-drift also auto-extracts constraint-like sentences from anywhere in your docs ("X should not...", "All Y must...", "Never Z...").

## Design Principles

1. **Claims, not facts.** Everything is a revisable belief with confidence and evidence.
2. **Verification threshold.** Claims don't enter shared memory unless they pass a bar.
3. **Scoped validity.** Every claim knows which files/commits/services it applies to.
4. **Typed edges.** Claims support, contradict, or supersede each other.
5. **Automatic decay.** Observations expire in 30 days. Decisions don't. Failures last 60 days.
6. **Zero-migration.** Reads CLAUDE.md you already have. Stores in SQLite. No setup.

## CLI Scanner

Scan any repo for drift without MCP setup:

```bash
# Local repo
python scripts/scan_repo.py /path/to/repo

# Remote repo (clones automatically)
python scripts/scan_repo.py https://github.com/org/repo

# Scope to specific path
python scripts/scan_repo.py . --scope src/auth

# JSON output
python scripts/scan_repo.py . --json
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT
