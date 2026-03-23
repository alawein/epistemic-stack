#!/usr/bin/env python3
"""Scan a local or remote repo for architectural drift.

Usage:
    python scripts/scan_repo.py /path/to/local/repo
    python scripts/scan_repo.py https://github.com/org/repo
    python scripts/scan_repo.py https://github.com/org/repo --scope src/auth
"""

from __future__ import annotations
import argparse, json, subprocess, sys, tempfile, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from importlib.util import spec_from_file_location, module_from_spec

def _load_drift():
    path = Path(__file__).resolve().parent.parent / "claude-drift" / "server.py"
    spec = spec_from_file_location("drift", str(path))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser(description="Scan a repo for architectural drift")
    parser.add_argument("target", help="Local path or git URL")
    parser.add_argument("--scope", help="Limit to files matching this path fragment")
    parser.add_argument("--json", dest="output_json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    drift = _load_drift()
    cleanup = False

    if args.target.startswith("http://") or args.target.startswith("https://") or args.target.startswith("git@"):
        tmpdir = tempfile.mkdtemp(prefix="drift-scan-")
        cleanup = True
        print(f"Cloning {args.target}...")
        r = subprocess.run(["git", "clone", "--depth=1", args.target, tmpdir],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Clone failed: {r.stderr}", file=sys.stderr)
            sys.exit(1)
        root = tmpdir
    else:
        root = str(Path(args.target).resolve())
        if not Path(root).is_dir():
            print(f"Not a directory: {root}", file=sys.stderr)
            sys.exit(1)

    try:
        # Scan intents
        intents = drift.scan_intents(root)
        # Run drift check
        result = drift.check_drift(root, scope=args.scope)
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)

    if args.output_json:
        print(json.dumps({"intents": intents, "drift": result}, indent=2))
        return

    # Human-readable report
    print(f"\n{'='*60}")
    print("  Drift Report")
    print(f"{'='*60}")
    print(f"  Intents found:    {intents['intents_found']}")
    print(f"  Actionable rules: {intents['actionable']}")
    print(f"  Unstructured:     {intents['needs_confirmation']}")
    print(f"  Files analyzed:   {result['files_analyzed']}")
    print(f"  Drift score:      {result['drift_score']}")
    print(f"  Violations:       {result['violation_count']}")
    if result.get("by_severity"):
        print(f"  By severity:      {result['by_severity']}")
    print(f"{'='*60}")

    if not result["violations"]:
        print("\n  No violations found.\n")
        return

    print()
    for v in result["violations"]:
        sev = v.get("severity", "?").upper()
        conf = v.get("confidence", 0)
        print(f"  [{sev}] {v['file']}:{v.get('line', '?')}  (confidence: {conf})")
        print(f"    Intent:   {v['intent_description']}")
        print(f"    Evidence: {v['evidence_text']}")
        if v.get("suggested_fix"):
            print(f"    Fix:      {v['suggested_fix']}")
        print()

    sys.exit(1 if result["violations"] else 0)


if __name__ == "__main__":
    main()
