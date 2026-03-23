"""Tests for claude-drift server — intent parsing and drift detection."""

from claude_drift_server import (
    _extract_intents_md, _extract_intents_json, _derive_rule,
    _check_import_boundary, _check_prohibition, _check_layer_enforcement,
    _find_intent_files, _drift_score, _intents,
    scan_intents, check_drift, will_this_drift, declare_intent,
)
from shared.types import ArchitecturalIntent, DriftViolation, Severity


# ── Intent Parsing ──────────────────────────────────────────────────────────

class TestIntentParsing:
    def test_extract_from_drift_block(self, tmp_project):
        intents = _extract_intents_md(tmp_project / "CLAUDE.md")
        descriptions = [i.description for i in intents]
        assert any("Auth" in d for d in descriptions)
        assert any("console.log" in d for d in descriptions)

    def test_extract_from_json(self, tmp_project):
        intents = _extract_intents_json(tmp_project / ".drift-rules.json")
        assert len(intents) == 1
        assert intents[0].rule_type == "import_boundary"
        assert intents[0].confirmed is True

    def test_find_intent_files(self, tmp_project):
        files = _find_intent_files(str(tmp_project))
        names = [f.name for f in files]
        assert "CLAUDE.md" in names
        assert ".drift-rules.json" in names

    def test_finds_adr_directory(self, tmp_project):
        adr_dir = tmp_project / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "001-use-jwt.md").write_text(
            "# ADR 001\n\n- All auth tokens must use JWT format\n"
        )
        files = _find_intent_files(str(tmp_project))
        assert any("001-use-jwt.md" in str(f) for f in files)


class TestRuleDerivation:
    def test_import_boundary(self):
        i = ArchitecturalIntent(description="Auth should not import from payment")
        _derive_rule(i)
        assert i.rule_type == "import_boundary"
        assert "auth" in i.rule_config["source_pattern"]
        assert "payment" in i.rule_config["forbidden_target"]

    def test_prohibition(self):
        i = ArchitecturalIntent(description="Never use console.log in src/production")
        _derive_rule(i)
        assert i.rule_type == "prohibition"
        assert "console.log" in i.rule_config["forbidden_action"]
        assert "src/production" in i.rule_config["scope"]

    def test_layer_enforcement(self):
        i = ArchitecturalIntent(
            description="All database access must go through the repository layer"
        )
        _derive_rule(i)
        assert i.rule_type == "layer_enforcement"
        assert "database access" in i.rule_config["consumer"]
        assert "repository layer" in i.rule_config["required_intermediary"]

    def test_unstructured_fallback(self):
        i = ArchitecturalIntent(description="Keep it simple")
        _derive_rule(i)
        assert i.rule_type == "unstructured"
        assert "raw" in i.rule_config


# ── Analyzers ───────────────────────────────────────────────────────────────

class TestImportBoundary:
    def test_detects_violation(self, tmp_project):
        intent = ArchitecturalIntent(
            description="Auth must not import payment",
            rule_type="import_boundary",
            rule_config={"source_pattern": "src/auth", "forbidden_target": "payment"},
        )
        violations = _check_import_boundary(intent, str(tmp_project))
        assert len(violations) >= 1
        assert any("handler.ts" in v.file for v in violations)
        assert violations[0].confidence == 0.91

    def test_no_false_positive(self, tmp_project):
        intent = ArchitecturalIntent(
            description="Payment must not import auth",
            rule_type="import_boundary",
            rule_config={"source_pattern": "src/payment", "forbidden_target": "auth"},
        )
        violations = _check_import_boundary(intent, str(tmp_project))
        assert len(violations) == 0


class TestProhibition:
    def test_detects_console_log(self, tmp_project):
        intent = ArchitecturalIntent(
            description="No console.log in api",
            rule_type="prohibition",
            rule_config={"forbidden_action": "console.log", "scope": "src/api"},
        )
        violations = _check_prohibition(intent, str(tmp_project))
        assert len(violations) >= 1
        assert any("controller.ts" in v.file for v in violations)

    def test_scoped_correctly(self, tmp_project):
        # console.log is only in src/api, not src/auth
        intent = ArchitecturalIntent(
            description="No console.log in auth",
            rule_type="prohibition",
            rule_config={"forbidden_action": "console.log", "scope": "src/auth"},
        )
        violations = _check_prohibition(intent, str(tmp_project))
        assert len(violations) == 0


class TestLayerEnforcement:
    def test_detects_direct_db_access(self, tmp_project):
        intent = ArchitecturalIntent(
            description="All API controllers must go through repo layer",
            rule_type="layer_enforcement",
            rule_config={"consumer": "src/api", "required_intermediary": "repo"},
        )
        violations = _check_layer_enforcement(intent, str(tmp_project))
        # controller.ts imports prisma directly instead of going through repo
        assert len(violations) >= 1
        assert any("controller.ts" in v.file for v in violations)

    def test_intermediary_files_excluded(self, tmp_project):
        intent = ArchitecturalIntent(
            description="All API must go through repo",
            rule_type="layer_enforcement",
            rule_config={"consumer": "src/repo", "required_intermediary": "repo"},
        )
        # repo files import prisma directly — that's fine, they ARE the intermediary
        violations = _check_layer_enforcement(intent, str(tmp_project))
        assert len(violations) == 0


# ── Drift Score ─────────────────────────────────────────────────────────────

class TestDriftScore:
    def test_zero_with_no_violations(self):
        score = _drift_score([], 10)
        assert score["drift_score"] == 0.0
        assert score["violation_count"] == 0

    def test_increases_with_severity(self):
        low = [DriftViolation(file="a.ts", severity=Severity.LOW, confidence=1.0)]
        high = [DriftViolation(file="a.ts", severity=Severity.HIGH, confidence=1.0)]
        s_low = _drift_score(low, 10)
        s_high = _drift_score(high, 10)
        assert s_high["drift_score"] > s_low["drift_score"]

    def test_zero_files_returns_zero(self):
        score = _drift_score([], 0)
        assert score["drift_score"] == 0.0


# ── MCP Tool Functions ──────────────────────────────────────────────────────

class TestScanIntents:
    def test_scans_project(self, tmp_project):
        _intents.clear()
        result = scan_intents(str(tmp_project))
        assert result["intents_found"] >= 3  # at least the 3 in CLAUDE.md + 1 in JSON
        assert result["actionable"] >= 1

    def test_specific_file(self, tmp_project):
        _intents.clear()
        result = scan_intents(str(tmp_project), str(tmp_project / ".drift-rules.json"))
        assert result["intents_found"] == 1


class TestCheckDrift:
    def test_finds_violations(self, tmp_project):
        _intents.clear()
        result = check_drift(str(tmp_project))
        assert result["violation_count"] >= 1
        assert result["drift_score"] > 0

    def test_scope_filter(self, tmp_project):
        _intents.clear()
        result = check_drift(str(tmp_project), scope="src/auth")
        # Only violations in auth scope
        for v in result["violations"]:
            assert "auth" in v["file"].lower()


class TestWillThisDrift:
    def test_warns_on_bad_import(self, tmp_project):
        _intents.clear()
        scan_intents(str(tmp_project))
        result = will_this_drift(
            "src/auth/handler.ts",
            'import { Charge } from "../payment/charge"',
            str(tmp_project),
        )
        assert not result["safe"]
        assert len(result["warnings"]) >= 1

    def test_safe_change(self, tmp_project):
        _intents.clear()
        scan_intents(str(tmp_project))
        result = will_this_drift(
            "src/auth/handler.ts",
            'import { Logger } from "../utils/logger"',
            str(tmp_project),
        )
        assert result["safe"]


class TestDeclareIntent:
    def test_auto_parse(self):
        _intents.clear()
        result = declare_intent("Models should not import from controllers")
        assert result["rule_type"] == "import_boundary"
        assert not result["needs_confirmation"]

    def test_explicit_config(self):
        _intents.clear()
        result = declare_intent(
            "No cross-domain", rule_type="import_boundary",
            source_pattern="src/a", forbidden_target="src/b",
        )
        assert result["confirmed"] is True
