"""
Verify stage1_sample.json reproducibility against raw baseline.

For each of the 5 sampled problems, run z3 with BASELINE params and compare:
  - result must match baseline_result
  - elapsed_ms ratio (got / baseline) must lie in [LOW, HIGH]

Per-problem timeout = baseline_ms (as recorded in raw data).
Single run per problem (no median).
"""
import json
import math
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "shared"))

from baseline_params import BASELINE  # noqa: E402
from z3_runner import run_z3  # noqa: E402

_BENCH_DIR = _HERE.parent
_RAW_DIR = _BENCH_DIR / "raw-data"
_PROBLEMS_JSONL = _BENCH_DIR / "problems.jsonl"
_STAGE1_SAMPLE = _HERE / "shared" / "stage1_sample.json"

RATIO_LOW = 0.5
RATIO_HIGH = 2.0


def _load_sample_shas():
    return list(json.loads(_STAGE1_SAMPLE.read_text())["sha256"])


def _index_problems_by_sha():
    idx = {}
    with open(_PROBLEMS_JSONL) as f:
        for line in f:
            d = json.loads(line)
            idx[d["problem_sha256"]] = {
                "smt2": d["smt2_filename"],
                "baseline_ms": d["z3_status"]["elapsed_ms"],
                "baseline_result": d["z3_status"]["result"],
            }
    return idx


def verify():
    shas = _load_sample_shas()
    idx = _index_problems_by_sha()

    rows = []
    fail = 0
    warn = 0
    for sha in shas:
        meta = idx.get(sha)
        if meta is None:
            print(f"[FAIL] {sha[:12]}  not in problems.jsonl")
            fail += 1
            continue

        smt2_path = _RAW_DIR / meta["smt2"]
        if not smt2_path.exists():
            print(f"[FAIL] {sha[:12]}  missing smt2 {smt2_path}")
            fail += 1
            continue

        timeout_s = max(1, math.ceil(meta["baseline_ms"] / 1000))
        r = run_z3(smt2_path, BASELINE, timeout_s)

        got_result = r.get("result", "Unknown")
        got_ms = int(r.get("elapsed_ms", 0))
        is_timeout = bool(r.get("timeout"))
        invalid = r.get("invalid_param")
        err = r.get("error")
        stderr = r.get("stderr")

        ratio = got_ms / max(meta["baseline_ms"], 1)
        result_ok = (got_result == meta["baseline_result"])
        ratio_ok = RATIO_LOW <= ratio <= RATIO_HIGH

        if invalid:
            status = "FAIL(invalid)"
            fail += 1
        elif err:
            status = "FAIL(error)"
            fail += 1
        elif is_timeout or not result_ok:
            status = "FAIL"
            fail += 1
        elif not ratio_ok:
            status = "WARN"
            warn += 1
        else:
            status = "OK"

        rows.append((sha, meta, got_result, got_ms, ratio, status, invalid, is_timeout, err, stderr))

    print()
    print(f"{'sha':<14}{'base_res':<10}{'got_res':<10}"
          f"{'base_ms':>10}{'got_ms':>10}{'ratio':>8}  status")
    print("-" * 78)
    for sha, meta, got_result, got_ms, ratio, status, invalid, is_timeout, err, stderr in rows:
        extra = ""
        if invalid:
            extra = f" invalid={invalid}"
        elif err:
            extra = f" err={err[:200]}"
        elif is_timeout:
            extra = " (timeout)"
        print(f"{sha[:12]:<14}{meta['baseline_result']:<10}{got_result:<10}"
              f"{meta['baseline_ms']:>10}{got_ms:>10}{ratio:>7.2f}x  {status}{extra}")
        if stderr and err:
            print(f"  stderr: {stderr[:400]}")

    print()
    print(f"summary: {len(rows) - fail - warn} ok, {warn} warn, {fail} fail "
          f"(ratio band [{RATIO_LOW}, {RATIO_HIGH}], timeout=baseline_ms)")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(verify())
