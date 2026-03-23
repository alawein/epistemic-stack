#!/usr/bin/env python3
"""
claude-drift: Architectural Intent Alignment Detection

Reads constraints from CLAUDE.md / ADRs / .drift-rules.json.
Derives enforceable rules from plain English.
Scores your architectural health.

  claude mcp add claude-drift -- python /path/to/claude-drift/server.py
"""

from __future__ import annotations
import json, re, subprocess, sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastmcp import FastMCP
from shared.types import ArchitecturalIntent, DriftViolation, IntentSource, Severity

mcp = FastMCP("claude-drift",
    instructions="Detects architectural drift — divergence between stated intent and actual code.")

_intents: dict[str, ArchitecturalIntent] = {}

# ── Intent Parsing ──────────────────────────────────────────────────────────

def _find_intent_files(root: str) -> list[Path]:
    root_path = Path(root).resolve()
    files = []
    for name in ["CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md"]:
        p = root_path / name
        if p.exists(): files.append(p)
    for adr_dir in ["docs/adr", "docs/adrs", "adr", "architecture/decisions"]:
        d = root_path / adr_dir
        if d.is_dir(): files.extend(sorted(d.glob("*.md")))
    rules = root_path / ".drift-rules.json"
    if rules.exists(): files.append(rules)
    return files


def _extract_intents_md(filepath: Path) -> list[ArchitecturalIntent]:
    text = filepath.read_text(encoding="utf-8", errors="replace")
    intents = []
    src = IntentSource.CLAUDE_MD if "CLAUDE" in filepath.name.upper() else IntentSource.ADR

    # Explicit markers
    for block in re.findall(r"<!--\s*drift:intent\s*-->(.*?)<!--\s*/drift:intent\s*-->", text, re.DOTALL | re.IGNORECASE):
        for line in block.strip().splitlines():
            line = line.strip().lstrip("- ").strip()
            if len(line) > 10:
                intents.append(ArchitecturalIntent(description=line, source=src, source_file=str(filepath)))

    # Constraint-like sentences anywhere
    for m in re.finditer(
        r"^[*\-]?\s*(?:(?:The|All|No|Every|Each)\s+.+?\s+(?:should|must|shall|never|always)\s+.+)$",
        text, re.IGNORECASE | re.MULTILINE
    ):
        desc = m.group(0).strip().lstrip("*- ").strip()
        if len(desc) > 15 and not any(i.description == desc for i in intents):
            intents.append(ArchitecturalIntent(description=desc, source=src, source_file=str(filepath)))

    # Lines under architecture headings
    for m in re.finditer(
        r"^#{1,4}\s+.*?(architect|boundar|rule|constraint|invariant|convention|must|never|always).*$",
        text, re.IGNORECASE | re.MULTILINE
    ):
        end = m.end()
        next_h = re.search(r"^#{1,4}\s+", text[end:], re.MULTILINE)
        section = text[end:end + next_h.start() if next_h else len(text)]
        for line in section.splitlines():
            s = line.strip().lstrip("- ").strip()
            if any(kw in s.lower() for kw in ["should","must","never","always","don't","forbidden","avoid","not import","not depend","go through"]) and len(s) > 15:
                if not any(i.description == s for i in intents):
                    intents.append(ArchitecturalIntent(description=s, source=src, source_file=str(filepath)))
    return intents


def _extract_intents_json(filepath: Path) -> list[ArchitecturalIntent]:
    data = json.loads(filepath.read_text())
    return [ArchitecturalIntent(
        description=r.get("description",""), source=IntentSource.DRIFT_RULES,
        source_file=str(filepath), rule_type=r.get("rule_type"),
        rule_config=r.get("config",{}), confirmed=True
    ) for r in data.get("rules",[])]


def _derive_rule(intent: ArchitecturalIntent) -> ArchitecturalIntent:
    desc = intent.description.lower()
    m = re.search(r"(\w[\w/.*-]+)\s+(?:should|must)\s+not\s+(?:import|depend on|use)\s+(?:from\s+)?(\w[\w/.*-]+)", desc)
    if m:
        intent.rule_type = "import_boundary"
        intent.rule_config = {"source_pattern": m.group(1), "forbidden_target": m.group(2)}
        return intent
    m = re.search(r"all\s+(\w[\w\s]*?)\s+(?:should|must)\s+(?:go through|use|access via)\s+(?:the\s+)?(\w[\w\s]*?)$", desc)
    if m:
        intent.rule_type = "layer_enforcement"
        intent.rule_config = {"consumer": m.group(1).strip(), "required_intermediary": m.group(2).strip()}
        return intent
    m = re.search(r"(?:never|do not|don't|avoid)\s+(.+?)\s+(?:in|inside|within)\s+(\w[\w/.*-]+)", desc)
    if m:
        intent.rule_type = "prohibition"
        intent.rule_config = {"forbidden_action": m.group(1).strip(), "scope": m.group(2).strip()}
        return intent
    intent.rule_type = "unstructured"
    intent.rule_config = {"raw": intent.description}
    return intent

# ── Analysis ────────────────────────────────────────────────────────────────

SKIP = {"node_modules",".git","__pycache__","dist","build",".next","venv",".venv","vendor"}
EXTS = (".ts",".tsx",".js",".jsx",".py",".go",".rs")

def _source_files(root: str) -> list[Path]:
    return [p for p in Path(root).resolve().rglob("*")
            if p.suffix in EXTS and p.is_file() and not any(s in p.parts for s in SKIP)]


def _rel_posix(fp: Path, root_path: Path) -> str:
    """Return a forward-slash relative path, consistent across OS."""
    return fp.relative_to(root_path).as_posix()


_IMPORT_PATTERNS = [
    re.compile(r"""from\s+['"]([^'"]+)['"]"""),                     # JS/TS: from "mod" / from 'mod'
    re.compile(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]\)?"""), # JS: require("mod"), import "mod"
    re.compile(r"from\s+([\w.]+)\s+import"),                        # Python: from mod import ...
    re.compile(r"import\s+([\w.]+)"),                                # Python/Go: import mod
]


def _extract_import(line: str) -> Optional[str]:
    """Return the imported module path from a source line, or None."""
    for pat in _IMPORT_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group(1)
    return None


def _check_import_boundary(intent: ArchitecturalIntent, root: str) -> list[DriftViolation]:
    cfg = intent.rule_config
    src_pat, forbidden = cfg.get("source_pattern","").lower(), cfg.get("forbidden_target","").lower()
    if not src_pat or not forbidden: return []
    violations = []
    root_path = Path(root).resolve()
    for fp in _source_files(root):
        rel = _rel_posix(fp, root_path)
        if src_pat.replace("*","") not in rel.lower(): continue
        try: content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception: continue
        for n, line in enumerate(content.splitlines(), 1):
            imported = _extract_import(line)
            if imported and forbidden in imported.lower():
                violations.append(DriftViolation(
                    intent_id=intent.id, intent_description=intent.description,
                    file=rel, line=n, evidence_text=line.strip(),
                    confidence=0.91, severity=Severity.HIGH,
                    suggested_fix=f"Remove dependency on {forbidden}; use DI or shared interface."))
    return violations


def _check_prohibition(intent: ArchitecturalIntent, root: str) -> list[DriftViolation]:
    cfg = intent.rule_config
    forbidden, scope = cfg.get("forbidden_action","").lower(), cfg.get("scope","").lower()
    if not forbidden or not scope: return []
    patterns_map = {
        "console.log": [r"console\.log\s*\("], "print": [r"\bprint\s*\("],
        "debugger": [r"\bdebugger\b"], "eval": [r"\beval\s*\("],
        "any type": [r":\s*any\b"], "todo": [r"//\s*TODO", r"#\s*TODO"],
    }
    patterns = []
    for k, v in patterns_map.items():
        if k in forbidden: patterns.extend(v)
    if not patterns: patterns = [re.escape(forbidden)]
    violations = []
    root_path = Path(root).resolve()
    for fp in _source_files(root):
        rel = _rel_posix(fp, root_path)
        if scope not in rel.lower(): continue
        try: content = fp.read_text(encoding="utf-8", errors="replace")
        except: continue
        for n, line in enumerate(content.splitlines(), 1):
            if any(re.search(p, line, re.IGNORECASE) for p in patterns):
                violations.append(DriftViolation(
                    intent_id=intent.id, intent_description=intent.description,
                    file=rel, line=n, evidence_text=line.strip(),
                    confidence=0.85, severity=Severity.MEDIUM,
                    suggested_fix=f"Remove prohibited: {forbidden}"))
    return violations


def _check_layer_enforcement(intent: ArchitecturalIntent, root: str) -> list[DriftViolation]:
    """Enforce 'all X must go through Y' by flagging imports that bypass the intermediary.

    Strategy: files matching 'consumer' should only reach the protected layer via
    'required_intermediary'.  Any direct import from common backing layers
    (db, sql, orm, http, filesystem, etc.) that doesn't go through the
    intermediary is a violation.
    """
    cfg = intent.rule_config
    consumer = cfg.get("consumer", "").lower()
    intermediary = cfg.get("required_intermediary", "").lower()
    if not consumer or not intermediary:
        return []

    # Common backing-layer keywords that the intermediary is meant to abstract
    DIRECT_ACCESS = [
        r"\bsqlalchemy\b", r"\bprisma\b", r"\bknex\b", r"\bsequelize\b",
        r"\btypeorm\b", r"\bmongoose\b", r"\bsqlite3\b", r"\bpg\b",
        r"\bmysql\b", r"\bredis\b", r"\bsql\b",
        r"\bopen\s*\(", r"\bfs\b", r"\bpath\b",
        r"\bfetch\s*\(", r"\baxios\b", r"\burllib\b", r"\brequests\b",
        r"\bhttpx\b", r"\baiohttp\b",
    ]

    violations = []
    root_path = Path(root).resolve()
    for fp in _source_files(root):
        rel = _rel_posix(fp, root_path)
        if consumer.replace("*", "") not in rel.lower():
            continue
        # Skip files that ARE the intermediary
        if intermediary.replace("*", "") in rel.lower():
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for n, line in enumerate(content.splitlines(), 1):
            # Check for direct imports of backing-layer modules
            imported = _extract_import(line)
            if not imported:
                continue
            imported = imported.lower()
            # If the import goes through the intermediary, it's fine
            if intermediary in imported:
                continue
            # Flag direct access to known backing layers
            if any(re.search(p, imported) for p in DIRECT_ACCESS):
                violations.append(DriftViolation(
                    intent_id=intent.id, intent_description=intent.description,
                    file=rel, line=n, evidence_text=line.strip(),
                    confidence=0.78, severity=Severity.HIGH,
                    suggested_fix=f"Access through {intermediary} instead of direct {imported}.",
                ))
    return violations


ANALYZERS = {
    "import_boundary": _check_import_boundary,
    "prohibition": _check_prohibition,
    "layer_enforcement": _check_layer_enforcement,
}


def _drift_score(violations: list[DriftViolation], file_count: int) -> dict:
    if not file_count:
        return {"drift_score": 0.0, "violation_count": 0, "files_analyzed": 0,
                "by_severity": {s.value: 0 for s in Severity}}
    weights = {"critical":4,"high":2,"medium":1,"low":0.5,"info":0.1}
    total = sum(weights.get(v.severity.value,1)*v.confidence for v in violations)
    return {
        "drift_score": round(min(1.0, total/(file_count*0.2)), 3),
        "violation_count": len(violations),
        "files_analyzed": file_count,
        "by_severity": {s.value: sum(1 for v in violations if v.severity==s) for s in Severity},
    }

# ── MCP Tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def scan_intents(root: str = ".", intent_file: Optional[str] = None) -> dict:
    """Discover and parse architectural intents from CLAUDE.md, ADRs, .drift-rules.json."""
    global _intents
    root = str(Path(root).resolve())
    files = [Path(intent_file).resolve()] if intent_file else _find_intent_files(root)
    all_intents = []
    for f in files:
        found = _extract_intents_json(f) if f.suffix == ".json" else _extract_intents_md(f)
        all_intents.extend(found)
    for i in all_intents:
        if not i.rule_type: _derive_rule(i)
        _intents[i.id] = i
    return {"intents_found": len(all_intents), "files_scanned": [str(f) for f in files],
            "intents": [i.to_dict() for i in all_intents],
            "actionable": sum(1 for i in all_intents if i.rule_type != "unstructured"),
            "needs_confirmation": sum(1 for i in all_intents if i.rule_type == "unstructured")}


@mcp.tool()
def declare_intent(description: str, rule_type: Optional[str]=None,
                   source_pattern: Optional[str]=None, forbidden_target: Optional[str]=None) -> dict:
    """Declare an architectural intent. NL is auto-parsed; or specify rule_type + config directly."""
    intent = ArchitecturalIntent(description=description, source=IntentSource.HUMAN_DECLARATION)
    if rule_type and source_pattern and forbidden_target:
        intent.rule_type, intent.rule_config = rule_type, {"source_pattern":source_pattern,"forbidden_target":forbidden_target}
        intent.confirmed = True
    else:
        _derive_rule(intent)
    _intents[intent.id] = intent
    return {"intent_id":intent.id, "rule_type":intent.rule_type, "rule_config":intent.rule_config,
            "confirmed":intent.confirmed, "needs_confirmation":intent.rule_type=="unstructured"}


@mcp.tool()
def check_drift(root: str = ".", scope: Optional[str] = None) -> dict:
    """Run drift detection against all registered intents. Returns violations + drift score."""
    root = str(Path(root).resolve())
    if not _intents: scan_intents(root)
    if not _intents:
        return {"error": "No intents found. Add constraints to CLAUDE.md or use declare_intent()."}
    violations = []
    for intent in _intents.values():
        analyzer = ANALYZERS.get(intent.rule_type)
        if analyzer:
            found = analyzer(intent, root)
            if scope: found = [v for v in found if scope.lower() in v.file.lower()]
            violations.extend(found)
    score = _drift_score(violations, len(_source_files(root)))
    git_head = subprocess.run(["git","rev-parse","HEAD"], capture_output=True, text=True, cwd=root).stdout.strip()
    return {**score, "git_head": git_head,
            "violations": [v.to_dict() for v in sorted(violations, key=lambda v: -v.confidence)],
            "intents_checked": len(_intents)}


@mcp.tool()
def check_drift_for_changes(root: str=".", base_commit: str="HEAD~1", head_commit: str="HEAD") -> dict:
    """Check drift introduced between two commits (e.g. a PR). Only analyzes changed files."""
    root = str(Path(root).resolve())
    changed = subprocess.run(["git","diff","--name-only",base_commit,head_commit],
                             capture_output=True, text=True, cwd=root).stdout.strip().splitlines()
    if not changed: return {"message":"No files changed","violations":[],"new_violations":0}
    if not _intents: scan_intents(root)
    violations = []
    for intent in _intents.values():
        analyzer = ANALYZERS.get(intent.rule_type)
        if analyzer: violations.extend(v for v in analyzer(intent, root) if v.file in changed)
    return {"base":base_commit,"head":head_commit,"files_changed":len(changed),
            "new_violations":len(violations),"violations":[v.to_dict() for v in violations]}


@mcp.tool()
def will_this_drift(file_path: str, proposed_change: str, root: str=".") -> dict:
    """Pre-flight: will a proposed change violate any intent? Call BEFORE editing."""
    if not _intents: scan_intents(root)
    warnings = []
    for intent in _intents.values():
        cfg = intent.rule_config
        if intent.rule_type == "import_boundary":
            if cfg.get("source_pattern","").lower() in file_path.lower() and cfg.get("forbidden_target","").lower() in proposed_change.lower():
                warnings.append({"intent":intent.description,"risk":"high",
                    "reason":f"Change may introduce dependency on {cfg['forbidden_target']}"})
        elif intent.rule_type == "prohibition":
            if cfg.get("scope","").lower() in file_path.lower() and cfg.get("forbidden_action","").lower() in proposed_change.lower():
                warnings.append({"intent":intent.description,"risk":"medium",
                    "reason":f"Change includes prohibited: {cfg['forbidden_action']}"})
    return {"file":file_path,"warnings":warnings,"safe": len(warnings)==0}


@mcp.tool()
def export_rules(root: str=".", output: str=".drift-rules.json") -> dict:
    """Export confirmed rules to .drift-rules.json for version control."""
    confirmed = [i for i in _intents.values() if i.confirmed]
    path = Path(root).resolve() / output
    path.write_text(json.dumps({"version":"1.0","rules":[
        {"id":i.id,"description":i.description,"rule_type":i.rule_type,"config":i.rule_config}
        for i in confirmed
    ]}, indent=2))
    return {"exported":len(confirmed),"file":str(path)}


if __name__ == "__main__":
    mcp.run()
