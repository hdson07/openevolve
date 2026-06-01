"""
Scan `raw-data/*.meta.jsonl` and emit `problems.jsonl` — one JSON per line,
sorted by SHA for deterministic diff.

Each `<sha>__<applied_params_hash>__seed<N>.meta.jsonl` file is a single JSON
object representing one baseline solver run. This script concatenates them
into the JSONL format `_lib.sampler` / `_lib.rebaseline` / `_lib.evaluator`
consume.

Usage:
    python3 input/z3-bench/build_problems.py [flags]

Flags:
    --filter-decisive             keep only Sat / Unsat rows
    --applied-params-hash HASH    restrict to one param profile (prefix match ok)
    --dry-run                     print counts but don't write
    --out PATH                    output path (default: problems.jsonl in bench root)

Output schema (one row per meta.jsonl entry):
    {
      "problem_sha256": "<sha>",
      "smt2_filename": "<sha>.smt2",
      "z3_status": {"result": "Sat", "elapsed_ms": 1234},
      "z3_statistics": {"conflicts": 100, ...},
      "z3_applied_params": {...},
      "features": {"num_hard_constraints": 24937, ...},
      "applied_params_hash": "...",
      "z3_version" / "solver" / "path" / ...
    }

Re-run any time raw-data/ changes. Idempotent — overwrites problems.jsonl.
"""
import argparse
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_RAW = _HERE / "raw-data"
_DEFAULT_OUT = _HERE / "problems.jsonl"

_DECISIVE = ("Sat", "Unsat")


def scan(raw_dir, *, filter_decisive=False, applied_hash_prefix=None):
    metas = sorted(raw_dir.glob("*.meta.jsonl"))
    if not metas:
        raise SystemExit(f"no *.meta.jsonl under {raw_dir}")
    rows = []
    bad = 0
    skipped_decisive = 0
    skipped_hash = 0
    for p in metas:
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            print(f"WARN: bad json {p.name}: {e}", file=sys.stderr)
            bad += 1
            continue
        if not isinstance(d, dict):
            bad += 1
            continue
        if "problem_sha256" not in d or "smt2_filename" not in d:
            print(f"WARN: missing required fields in {p.name}", file=sys.stderr)
            bad += 1
            continue
        if applied_hash_prefix and not str(d.get("applied_params_hash", ""))\
                .startswith(applied_hash_prefix):
            skipped_hash += 1
            continue
        if filter_decisive:
            res = (d.get("z3_status") or {}).get("result")
            if res not in _DECISIVE:
                skipped_decisive += 1
                continue
        rows.append(d)
    rows.sort(key=lambda r: (r["problem_sha256"], r.get("applied_params_hash", "")))
    return rows, {"bad": bad, "skipped_decisive": skipped_decisive,
                  "skipped_hash": skipped_hash, "scanned": len(metas)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--filter-decisive", action="store_true",
                    help="keep only Sat / Unsat rows")
    ap.add_argument("--applied-params-hash", type=str, default=None,
                    help="restrict to applied_params_hash starting with this prefix")
    ap.add_argument("--dry-run", action="store_true",
                    help="print counts but don't write")
    ap.add_argument("--out", type=pathlib.Path, default=_DEFAULT_OUT,
                    help=f"output path (default: {_DEFAULT_OUT.name})")
    args = ap.parse_args()

    rows, stats = scan(_RAW,
                       filter_decisive=args.filter_decisive,
                       applied_hash_prefix=args.applied_params_hash)
    print(f"scanned {stats['scanned']} meta.jsonl files")
    if stats["bad"]:
        print(f"  skipped {stats['bad']} malformed")
    if stats["skipped_hash"]:
        print(f"  skipped {stats['skipped_hash']} non-matching applied_params_hash")
    if stats["skipped_decisive"]:
        print(f"  skipped {stats['skipped_decisive']} non-decisive baselines")
    print(f"  kept {len(rows)} rows")

    if args.dry_run:
        print("(dry-run — no write)")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    try:
        rel = args.out.relative_to(_HERE.parent)
    except ValueError:
        rel = args.out
    print(f"wrote {rel} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
