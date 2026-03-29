---
type: canonical
source: none
sync: none
sla: none
---

# Agent Governance — fixpoint

## Scope

Three MCP servers (claude-drift, claude-memory-mesh, claude-proof) for justified, revisable beliefs in AI-assisted development.

## Directory Layout

| Path | Governance |
|------|-----------|
| claude-drift/ | MCP server — drift detection |
| claude-memory-mesh/ | MCP server — persistent beliefs |
| claude-proof/ | MCP server — verification gate |
| shared/ | Common utilities |
| tests/ | Test suites |
| docs/ | Documentation |
| examples/ | Usage examples |

## Invariants

- All tests pass: `pytest tests/ -v`
- Linting passes: `ruff check .`
- Each MCP server is independently runnable
- Memory mesh entries require evidence and decay metadata
- Proof gate must verify before writing to memory

## Agent Rules

- Never bypass the proof gate for memory writes
- Always include evidence references in belief entries
- Respect decay policies — stale beliefs must be flagged
- Drift scores are advisory, not blocking