"""
Generate stage1_sample.json: 5 fastest Sat problems (fallback: fastest Unsat
to fill remainder).

Rationale: stage1 has tight per-problem timeout (15s default). Including slow
baseline problems (>15s) guarantees timeout regardless of variant quality,
making stage1 score uninformative. Sat is preferred since LLM is more likely
to preserve Sat than Unsat under aggressive param changes (Unsat proofs depend
on completeness of preprocessing pipeline).

No randomness — deterministic pick of fastest N by result class.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
PROBLEMS = ROOT.parent / "problems.jsonl"
OUT = ROOT / "shared" / "stage1_sample.json"

NUM_PROBLEMS = 5
# Stage1 wall-clock budget: 60-120s per variant.
#   - good variant (baseline-like): 5 × ~12s = 60s lower bound
#   - bad variant (all timeout):     5 × 24s  = 120s upper bound
# Pick problems whose baseline elapsed_ms falls in [MIN_MS, MAX_MS].
# Per-problem timeout defaults to 24s in evaluator.py.
MIN_MS = 4000
MAX_MS = 15000


def main():
    rows = []
    with open(PROBLEMS) as f:
        for line in f:
            d = json.loads(line)
            rows.append(
                (
                    d["problem_sha256"],
                    d["z3_status"]["elapsed_ms"],
                    d["z3_status"]["result"],
                )
            )
    rows.sort(key=lambda r: r[1])  # ascending by elapsed_ms

    in_range = [r for r in rows if MIN_MS <= r[1] <= MAX_MS]
    sat_band = [r for r in in_range if r[2] == "Sat"]
    unsat_band = [r for r in in_range if r[2] == "Unsat"]

    picked = sat_band[:NUM_PROBLEMS]
    if len(picked) < NUM_PROBLEMS:
        picked.extend(unsat_band[: NUM_PROBLEMS - len(picked)])
    if len(picked) < NUM_PROBLEMS:
        # fallback: widen below MIN_MS toward 0, prefer Sat
        below = [r for r in rows if r[1] < MIN_MS]
        sat_below = sorted((r for r in below if r[2] == "Sat"), key=lambda r: -r[1])
        picked.extend(sat_below[: NUM_PROBLEMS - len(picked)])
    if len(picked) < NUM_PROBLEMS:
        remaining = [r for r in rows if r not in picked]
        picked.extend(remaining[: NUM_PROBLEMS - len(picked)])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "selection": f"{NUM_PROBLEMS} Sat-preferred with baseline_ms in [{MIN_MS}, {MAX_MS}] (Unsat / sub-MIN_MS fallbacks)",
                "source": str(PROBLEMS.relative_to(ROOT.parent.parent)),
                "sha256": [r[0] for r in picked],
                "summary": [
                    {
                        "sha": r[0][:12],
                        "baseline_ms": r[1],
                        "baseline_result": r[2],
                    }
                    for r in picked
                ],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {OUT.relative_to(ROOT.parent.parent)} ({len(picked)} problems)")
    for s in picked:
        print(f"  {s[0][:12]}  {s[1]:>6} ms  {s[2]}")


if __name__ == "__main__":
    main()
