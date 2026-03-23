# Epistemic Stack — Master Prompt

## What This Is

Three MCP servers that give AI coding agents justified, revisable beliefs about your codebase.

```
claude-drift        → detects divergence between intent and implementation
claude-memory-mesh  → persists what agents believe (with evidence and decay)
claude-proof        → verifies changes and gates what enters memory
```

Without proof, memory is noisy. Without memory, proof is local and wasted. Without drift detection, neither stays aligned with reality.

---

## Why This Matters Right Now

AI coding tools (Claude Code, Cursor, Copilot) are stateless by session, blind to architectural intent, and unable to distinguish a tested invariant from a hallucinated guess. This creates three measurable failures:

1. **Repeated reasoning** — agents rediscover the same constraint every session
2. **Contradictory decisions** — isolated agents form incompatible models of the same code
3. **Invisible architectural drift** — code diverges from intent and nobody measures it

Current memory solutions (CLAUDE.md, Cursor rules, Windsurf memories) are flat files with no relationships, no evidence, no scoped validity, no contradiction detection, and no decay. After a few weeks they become landfills.

**The real bottleneck isn't coordination — it's that agents have no mechanism for forming justified, revisable beliefs.** They can't distinguish a guess from a tested invariant. They can't know when a belief expired because the code changed.

---

## The Three Layers

### Layer 1: claude-drift (Detection)

Reads architectural intent from CLAUDE.md, ADRs, or `.drift-rules.json`. Derives enforceable rules from natural language. Scans code for violations. Produces a drift score.

**Key tools:** `scan_intents`, `check_drift`, `will_this_drift`, `check_drift_for_changes`

**Example:** Your CLAUDE.md says "Auth should not import from payment." claude-drift finds `import { PaymentSession } from '../payment/session'` on line 47 of auth/handler.ts and flags it at 0.91 confidence.

### Layer 2: claude-memory-mesh (Persistence)

Graph-structured memory where claims need evidence to enter. Typed edges (supports / contradicts / supersedes). Bi-temporal tracking. Automatic decay rules per claim type.

**Key tools:** `store_claim`, `before_modifying`, `record_decision`, `record_failure`, `query_claims`

**Example:** Before editing auth/handler.ts, call `before_modifying("auth/handler.ts")`. Returns: 2 constraints, 1 past failure ("async refactor broke token refresh due to sync ORM dependency"), 1 decision ("chose JWT over sessions for cross-service auth").

### Layer 3: claude-proof (Commitment)

Wraps code modifications in verification chains. Every edit declares intent, gets checkpointed, tested, and recorded. Produces proof artifacts. Verified claims flow into memory mesh.

**Key tools:** `begin_modification`, `checkpoint`, `verify_step`, `rollback`, `finalize_proof`

**Example:** Start a proof chain for migrating auth to async. Run tests at each step. If step 3 fails, rollback to checkpoint 2. When done, `finalize_proof()` outputs claims ready for `store_claim()`.

---

## How They Compose

```
Code Change → claude-proof verifies it → verified claims promoted to claude-memory-mesh
                                                         ↕
              claude-drift checks ← intents from CLAUDE.md + memory graph
```

The graph grows. Claims get evidence. Evidence gets verified. Verified claims inform future decisions. Stale claims decay. Contradictions surface.

---

## Setup

```bash
pip install fastmcp

# Add to Claude Code (update paths)
claude mcp add claude-drift -- python /path/to/claude-drift/server.py
claude mcp add claude-memory-mesh -- python /path/to/claude-memory-mesh/server.py
claude mcp add claude-proof -- python /path/to/claude-proof/server.py
```

---

## Design Principles

1. **Claims, not facts.** Everything is a revisable belief with confidence and evidence.
2. **Verification threshold.** Claims don't enter shared memory unless they pass a bar.
3. **Scoped validity.** Every claim knows which files/commits/services it applies to.
4. **Bi-temporal tracking.** When observed vs. when valid are different questions.
5. **Typed edges.** Claims support, contradict, or supersede each other.
6. **Automatic decay.** Observations expire in 30 days. Decisions don't. Failures last 60 days.
7. **Zero-migration.** Reads CLAUDE.md you already have. Stores in SQLite. No setup.

---

## Research Context

- **ContextCov** (arXiv:2603.00822) — transforms passive coding instructions into active guardrails
- **Graphiti/Zep** (arXiv:2501.13956) — temporal knowledge graphs for agent memory
- **ArchUnit** — architectural conformance checking (language-specific, rule-based)
- **SLSA** — supply chain provenance adapted for AI code changes
- **A-MEM** (arXiv:2502.12110) — Zettelkasten-inspired agentic memory

**The gap:** No existing tool combines NL intent parsing + multi-language drift detection + verified graph memory + proof chains in a single composable MCP system.

---

## Go-to-Market

**Week 1-2:** Ship claude-drift standalone. Run against 5 OSS projects. Post "Show HN: I built a tool that measures how far your codebase has drifted from your own stated intentions."

**Week 3-6:** Add memory-mesh and proof. GitHub Action for PR drift checks. Submit to MCP directories (PulseMCP, GitHub MCP Registry, Glama).

**Week 7-12:** Run drift velocity experiment on 50 OSS projects (before/after AI tool adoption). Draft FSE/ASE paper. Pilot with 2 enterprise teams.

**Funding thesis:** Every enterprise shipping AI-generated code faces an audit question they can't answer: how do you know what your AI changed, why, and whether it violated constraints? EU AI Act enforcement started Aug 2025. 77% of orgs building AI governance. This is the tooling that makes compliance tractable.

---

## Competitive Position

| Tool | Misses |
|------|--------|
| ArchUnit | Java-only, DSL rules, no NL, no memory |
| Semgrep | Pattern matching, no intent, no memory, no proofs |
| Qodo | AI code review, no persistent memory, no drift scoring |
| CLAUDE.md | Flat file, no graph, no evidence, no decay |
| Omega Memory | No code awareness, no architectural intent |

**Our position:** Only system combining NL intent → enforceable rules → verified graph memory → proof chains, delivered as composable MCP servers for every AI coding tool.
