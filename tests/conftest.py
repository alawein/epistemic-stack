"""Shared fixtures for epistemic-stack tests."""

import importlib.util
import sys
from pathlib import Path

import pytest

# Ensure repo root is importable (for shared.types)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load hyphenated server modules under clean names so tests can import them.
# e.g. `from claude_drift_server import scan_intents`
for dirname, modname in [
    ("claude-drift", "claude_drift_server"),
    ("claude-memory-mesh", "claude_memory_mesh_server"),
    ("claude-proof", "claude_proof_server"),
]:
    path = ROOT / dirname / "server.py"
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project tree with architectural violations for testing.

    Layout:
        src/auth/handler.ts    -- imports from payment (violation)
        src/payment/session.ts -- payment module
        src/api/controller.ts  -- has console.log (violation) + direct DB import (layer violation)
        src/repo/user_repo.ts  -- the "repository layer" intermediary
        CLAUDE.md              -- contains drift:intent block
        .drift-rules.json      -- structured rules
    """
    # CLAUDE.md with intent markers
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# Project\n\n"
        "<!-- drift:intent -->\n"
        "- Auth module should not import from payment domain\n"
        "- All API controllers must go through the repository layer\n"
        "- Never use console.log in src/api\n"
        "<!-- /drift:intent -->\n"
    )

    # Structured rules
    rules = tmp_path / ".drift-rules.json"
    rules.write_text(
        '{"version":"1.0","rules":['
        '{"id":"r1","description":"Auth must not import payment",'
        '"rule_type":"import_boundary","config":{"source_pattern":"src/auth","forbidden_target":"payment"}}'
        ']}'
    )

    # Source files
    auth_dir = tmp_path / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "handler.ts").write_text(
        'import { PaymentSession } from "../payment/session";\n'
        'import { User } from "../models/user";\n'
        'export function handleAuth() { return true; }\n'
    )

    pay_dir = tmp_path / "src" / "payment"
    pay_dir.mkdir(parents=True)
    (pay_dir / "session.ts").write_text(
        'export class PaymentSession { id: string = ""; }\n'
    )

    api_dir = tmp_path / "src" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "controller.ts").write_text(
        'import { PrismaClient } from "prisma";\n'
        'console.log("debug");\n'
        'export function getUsers() { return []; }\n'
    )

    repo_dir = tmp_path / "src" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "user_repo.ts").write_text(
        'import { PrismaClient } from "prisma";\n'
        'export function findUser(id: string) { return null; }\n'
    )

    return tmp_path


@pytest.fixture
def memory_db(tmp_path):
    """Return a path for a temporary SQLite database."""
    return str(tmp_path / "test-memory.db")
