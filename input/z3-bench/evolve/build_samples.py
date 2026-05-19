"""
Build problems.jsonl + stage1/2/3 sample files from raw-data.

Source of truth = raw-data/*.meta.jsonl. raw-data accumulates over time;
this script rescans each run, rewrites problems.jsonl as the full aggregate,
and re-selects all stage samples.

Sample pool cap: baseline_ms <= MAX_BASELINE_MS (5 min). problems.jsonl
still contains the full set; only sample selection applies the cap.

Stage1 (5):  SAT, runtime lower-half (fast-medium). Quintile-spread by
             baseline_ms within the half. Drives the fast evolve loop —
             keeps per-iteration wall-clock bounded.
Stage2 (5):  SAT, runtime upper-half (medium-slow) capped at
             STAGE2_MAX_MS (2 min). Quintile-spread by baseline_ms within
             the capped pool. Catches regressions on harder instances
             without letting stage2 wall-clock blow up.
Stage3 (50): SAT+UNSAT, quintile-spread by size
             (num_hard_constraints, num_variables). Broad coverage for
             final verification / full-dataset scoring.

Quintile-spread = sort by key, split into 5 equal-rank buckets, pick N/5
from each bucket via rank-linspace within bucket. Deterministic, no
randomness.
"""
import json
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_BENCH = _HERE.parent
_RAW = _BENCH / "raw-data"
_PROBLEMS = _BENCH / "problems.jsonl"
_STAGE1 = _HERE / "shared" / "stage1_sample.json"
_STAGE2 = _HERE / "shared" / "stage2_sample.json"
_STAGE3 = _HERE / "shared" / "stage3_sample.json"

STAGE1_N = 5
STAGE2_N = 5
STAGE3_N = 50
N_BUCKETS = 5
MAX_BASELINE_MS = 300_000  # 5 min cap — exclude monster problems from sample pool
STAGE2_MAX_MS = 120_000    # 2 min cap — stage2 wall must stay bounded


def _scan_raw():
    rows = []
    for path in sorted(_RAW.glob("*.meta.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
                break
    return rows


def _size_key(d):
    feats = d.get("features") or {}
    return (feats.get("num_hard_constraints", 0), feats.get("num_variables", 0))


def _runtime_key(d):
    return (d.get("z3_status") or {}).get("elapsed_ms", 0)


def _quintile_spread(sorted_rows, n_pick, n_buckets=N_BUCKETS):
    total = len(sorted_rows)
    if total == 0 or n_pick <= 0:
        return []
    if total <= n_pick:
        return list(sorted_rows)

    per_bucket = n_pick // n_buckets
    remainder = n_pick % n_buckets
    picked = []
    for b in range(n_buckets):
        lo = (b * total) // n_buckets
        hi = ((b + 1) * total) // n_buckets
        bucket = sorted_rows[lo:hi]
        if not bucket:
            continue
        k = per_bucket + (1 if b < remainder else 0)
        if k <= 0:
            continue
        if k == 1:
            picked.append(bucket[len(bucket) // 2])
        else:
            for j in range(k):
                idx = round(j * (len(bucket) - 1) / (k - 1))
                picked.append(bucket[idx])
    return picked


def _summary(d):
    f = d.get("features") or {}
    z = d.get("z3_status") or {}
    return {
        "sha": d["problem_sha256"][:12],
        "num_hard_constraints": f.get("num_hard_constraints", 0),
        "num_variables": f.get("num_variables", 0),
        "baseline_result": z.get("result"),
        "baseline_ms": z.get("elapsed_ms"),
    }


def _write_sample(path, picks, label, criteria):
    path.write_text(
        json.dumps(
            {
                "selection": f"{len(picks)} {criteria}, quintile-spread by "
                             "(num_hard_constraints, num_variables)",
                "source": str(_PROBLEMS.relative_to(_BENCH.parent)),
                "sha256": [d["problem_sha256"] for d in picks],
                "summary": [_summary(d) for d in picks],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {path.relative_to(_BENCH.parent)} ({len(picks)} {label})")


def main():
    rows = _scan_raw()
    if not rows:
        raise SystemExit(f"no *.meta.jsonl files found under {_RAW}")
    print(f"scanned {len(rows)} raw meta files")

    with open(_PROBLEMS, "w") as f:
        for d in rows:
            f.write(json.dumps(d) + "\n")
    print(f"wrote {_PROBLEMS.relative_to(_BENCH.parent)} ({len(rows)} entries)")

    candidates = [
        d for d in rows
        if (d.get("z3_status") or {}).get("elapsed_ms", 0) <= MAX_BASELINE_MS
    ]
    skipped = len(rows) - len(candidates)
    print(f"sample pool: {len(candidates)} (skipped {skipped} with "
          f"baseline_ms > {MAX_BASELINE_MS}ms)")

    # Stage1/2: SAT only, split by runtime median.
    sat_by_rt = sorted(
        (d for d in candidates if (d.get("z3_status") or {}).get("result") == "Sat"),
        key=_runtime_key,
    )
    half = len(sat_by_rt) // 2
    sat_lower = sat_by_rt[:half]              # fast-medium runtime
    sat_upper = sat_by_rt[half:]              # medium-slow runtime
    sat_upper_capped = [d for d in sat_upper if _runtime_key(d) <= STAGE2_MAX_MS]
    print(f"stage2 pool: {len(sat_upper_capped)} (capped at {STAGE2_MAX_MS}ms, "
          f"dropped {len(sat_upper) - len(sat_upper_capped)} from upper-half)")

    s1 = _quintile_spread(sat_lower, STAGE1_N, N_BUCKETS)
    s2 = _quintile_spread(sat_upper_capped, STAGE2_N, N_BUCKETS)

    # Stage3: SAT+UNSAT, quintile-spread by problem size (broad coverage).
    s3 = _quintile_spread(sorted(candidates, key=_size_key), STAGE3_N, N_BUCKETS)

    _write_sample(_STAGE1, s1, "stage1", "SAT runtime lower-half")
    _write_sample(_STAGE2, s2, "stage2", "SAT runtime upper-half")
    _write_sample(_STAGE3, s3, "stage3", "SAT+UNSAT broad-size")

    for label, picks in (("stage1", s1), ("stage2", s2), ("stage3", s3)):
        print(f"\n{label}:")
        for d in picks:
            f_ = d.get("features") or {}
            z = d.get("z3_status") or {}
            print(
                f"  {d['problem_sha256'][:12]}  "
                f"hc={f_.get('num_hard_constraints', 0):>7}  "
                f"vars={f_.get('num_variables', 0):>7}  "
                f"{z.get('result', '?'):<7}  "
                f"{z.get('elapsed_ms', 0):>6}ms"
            )


if __name__ == "__main__":
    main()
