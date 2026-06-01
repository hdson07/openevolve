"""
Generic BASELINE stage1 sanity test. Runs each stage1 problem with the
catalog `defaults` once and reports ratio vs the recorded baseline_ms.

CLI: `python -m _lib.self_test <bench>`

OK   ratio in [0.5, 2.0]
WARN ratio out of band
FAIL result mismatch / invalid_param
"""
import argparse
import json
import math
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from _lib import bench_paths, params_catalog, runtime, subprocess_runner


def run_self_test(bench_root):
    bench_root = pathlib.Path(bench_root).resolve()
    adapter = bench_paths.load_adapter(bench_root)
    catalog = params_catalog.load_for_bench(bench_root)
    cache = bench_paths.cache_dir(bench_root)
    raw_dir = bench_paths.raw_dir(bench_root)
    problems_jsonl = bench_paths.problems_jsonl(bench_root)
    worker = bench_paths.worker_path(bench_root)

    stage1 = cache / "stage1_sample.json"
    if not stage1.exists():
        print(f"ERROR: {stage1} missing — run sampler first", file=sys.stderr)
        return 2

    shas = list(json.loads(stage1.read_text())["sha256"])
    idx = {}
    with open(problems_jsonl) as f:
        for line in f:
            d = json.loads(line)
            status = d.get(adapter.STATUS_FIELD) or {}
            idx[d["problem_sha256"]] = {
                "input_file": d[adapter.PROBLEM_FILE_FIELD],
                "baseline_ms": status.get("elapsed_ms", 0),
                "baseline_result": status.get("result"),
            }

    tasks = []
    for i, sha in enumerate(shas):
        meta = idx.get(sha)
        if meta is None:
            print(f"ERROR: {sha[:12]} missing from problems.jsonl", file=sys.stderr)
            return 2
        path = raw_dir / meta["input_file"]
        if not path.exists():
            print(f"ERROR: input missing: {path}", file=sys.stderr)
            return 2
        tasks.append((i, sha, meta, path))

    cores = runtime.core_range()
    if cores is None:
        cores = list(range(1, runtime.parallel_solvers(
            bench_paths.config_path(bench_root), default=5) + 1))
    n_parallel = min(len(cores), len(tasks))
    cores = cores[:n_parallel]

    print(f"BASELINE self-test ({adapter.SOLVER_NAME}): {len(tasks)} stage1 "
          f"problems, parallel={n_parallel} cores={cores}")
    print()

    baseline = dict(catalog.defaults)

    def _solve(t):
        i, sha, meta, path = t
        timeout_s = max(30, math.ceil(meta["baseline_ms"] * 2 / 1000))
        core = cores[i % n_parallel] if cores else None
        return i, sha, meta, subprocess_runner.run_solver(
            worker_path=worker, problem_path=path, params=baseline,
            timeout_s=timeout_s, cpu_core=core)

    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=n_parallel) as ex:
        futs = [ex.submit(_solve, t) for t in tasks]
        for f in as_completed(futs):
            results.append(f.result())
    elapsed = time.monotonic() - t0
    results.sort(key=lambda x: x[0])

    print(f"{'sha':<14}{'base_res':<10}{'got_res':<10}"
          f"{'base_ms':>10}{'got_ms':>10}{'ratio':>8}  core  status")
    print("-" * 84)
    fail = warn = 0
    for i, sha, meta, r in results:
        got = r.get("result", "Unknown")
        got_ms = int(r.get("elapsed_ms", 0))
        ratio = got_ms / max(meta["baseline_ms"], 1)
        invalid = r.get("invalid_param")
        if invalid:
            status = f"FAIL(invalid={invalid})"
            fail += 1
        elif got != meta["baseline_result"]:
            status = "FAIL"
            fail += 1
        elif not (0.5 <= ratio <= 2.0):
            status = "WARN"
            warn += 1
        else:
            status = "OK"
        print(f"{sha[:12]:<14}{str(meta['baseline_result']):<10}{str(got):<10}"
              f"{int(meta['baseline_ms']):>10}{got_ms:>10}{ratio:>7.2f}x  "
              f"{cores[i % n_parallel]:>4}  {status}")

    print()
    print(f"wall-clock: {elapsed:.1f}s")
    print(f"summary: {len(results) - fail - warn} ok, {warn} warn, {fail} fail")
    return 0 if fail == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bench", help="bench dir name (e.g. cpsat-bench)")
    args = ap.parse_args(argv)
    sys.exit(run_self_test(bench_paths.resolve_bench(args.bench)))


if __name__ == "__main__":
    main()
