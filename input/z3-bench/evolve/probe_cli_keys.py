"""
Probe which keys from shared/z3_valid_keys.json the Z3 CLI actually accepts
as positional `key=value` arguments. Some keys exist in `-pm:<mod>` doc but
are rejected by the CLI (structural / API-only options).

For each key, run:
    z3 -smt2 <key>=<default_value> <trivial_smt2>
Capture stderr -> classify as CLI_OK / CLI_REJECT.

Writes shared/z3_cli_skip_keys.json with the list to skip.

Run inside the container (z3 binary required). This is slow (~N seconds for
N ~1000 keys, ~1s per probe). Use --limit to test a subset first.
"""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
SHARED = ROOT / "shared"

# Trivial SMT2: instant decide.
_TRIVIAL_SMT2 = "(declare-const x Bool)\n(assert x)\n(check-sat)\n"

_REJECT_RES = [
    re.compile(r"unknown\s+parameter", re.IGNORECASE),
    re.compile(r"invalid\s+parameter", re.IGNORECASE),
    re.compile(r"unknown\s+option", re.IGNORECASE),
    re.compile(r"error\s+setting", re.IGNORECASE),
    re.compile(r"is\s+a\s+structural\s+parameter", re.IGNORECASE),
]


def is_rejection(stderr):
    for rx in _REJECT_RES:
        if rx.search(stderr):
            return True
    return False


def probe(key, value, smt2_path, z3_bin):
    """Return True if CLI rejects this key."""
    args = [z3_bin, "-smt2", "-T:5", f"{key}={value}", smt2_path]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False  # timeout != reject; key was accepted but eval slow
    return is_rejection(proc.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="probe at most N keys (0 = all)")
    ap.add_argument("--z3-bin", default="z3")
    args = ap.parse_args()

    if shutil.which(args.z3_bin) is None:
        print(f"ERROR: {args.z3_bin} not on PATH", file=sys.stderr)
        sys.exit(1)

    valid_keys_path = SHARED / "z3_valid_keys.json"
    if not valid_keys_path.exists():
        print(f"ERROR: {valid_keys_path} missing. Run validate_keys.py first.", file=sys.stderr)
        sys.exit(1)
    keys = sorted(json.loads(valid_keys_path.read_text()))

    # Use a safe sentinel value: integer 0 works for bool/uint/int/double;
    # for symbol-typed options z3 ignores numeric mismatches but typically
    # doesn't error at the parse level. Falls back to "false" if 0 rejected.
    candidate_values = ["0", "false"]

    with tempfile.NamedTemporaryFile("w", suffix=".smt2", delete=False) as f:
        f.write(_TRIVIAL_SMT2)
        smt2 = f.name

    rejects = []
    total = len(keys) if args.limit == 0 else min(args.limit, len(keys))
    print(f"probing {total} keys with z3={args.z3_bin}")
    for i, k in enumerate(keys[:total], 1):
        rejected_all = True
        for v in candidate_values:
            if not probe(k, v, smt2, args.z3_bin):
                rejected_all = False
                break
        if rejected_all:
            rejects.append(k)
        if i % 50 == 0:
            print(f"  {i}/{total}  rejects so far: {len(rejects)}")

    out = SHARED / "z3_cli_skip_keys.json"
    out.write_text(json.dumps(sorted(rejects), indent=2) + "\n")
    print(f"\nwrote {out.relative_to(ROOT)} ({len(rejects)} CLI-rejected keys)")
    if rejects:
        print("examples:")
        for r in rejects[:20]:
            print(f"  - {r}")


if __name__ == "__main__":
    main()
