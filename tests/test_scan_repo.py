"""Tests for scripts/scan_repo.py — CLI drift scanner."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scan_repo import _load_drift, main  # noqa: E402


class TestLoadDrift:
    def test_loads_module_with_scan_intents(self):
        mod = _load_drift()
        assert hasattr(mod, "scan_intents")
        assert hasattr(mod, "check_drift")


class TestScanLocalRepo:
    def test_reports_violations(self, tmp_project, capsys):
        with patch("sys.argv", ["scan_repo.py", str(tmp_project)]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1  # violations found
        out = capsys.readouterr().out
        assert "Drift Report" in out
        assert "Violations" in out

    def test_clean_project_exits_zero(self, tmp_path, capsys):
        # Project with intents but no violations
        (tmp_path / "CLAUDE.md").write_text(
            "<!-- drift:intent -->\n"
            "- Auth module should not import from billing\n"
            "<!-- /drift:intent -->\n"
        )
        (tmp_path / "src" / "auth").mkdir(parents=True)
        (tmp_path / "src" / "auth" / "handler.ts").write_text(
            'import { Logger } from "../utils/logger";\n'
        )
        with patch("sys.argv", ["scan_repo.py", str(tmp_path)]):
            main()  # should not raise SystemExit(1)

    def test_json_output(self, tmp_project, capsys):
        with patch("sys.argv", ["scan_repo.py", str(tmp_project), "--json"]):
            main()  # JSON mode doesn't sys.exit on violations
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "intents" in data
        assert "drift" in data
        assert data["drift"]["violation_count"] >= 1

    def test_scope_filter(self, tmp_project, capsys):
        with patch("sys.argv", ["scan_repo.py", str(tmp_project), "--scope", "src/auth", "--json"]):
            main()
        out = capsys.readouterr().out
        data = json.loads(out)
        for v in data["drift"]["violations"]:
            assert "auth" in v["file"].lower()

    def test_invalid_path(self, capsys):
        with patch("sys.argv", ["scan_repo.py", "/nonexistent/path/xyz"]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
