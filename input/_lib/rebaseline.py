"""
Per-host baseline measurement. Runs BASELINE params over the union of
`cache/stage{1..4}_sample.json` and writes `cache/local_baseline.json`.

CLI: `python -m _lib.rebaseline <bench> [--workers 1,8]`

Default worker counts:
  - If `adapter.WORKERS_KEY` is set (e.g. cpsat: "num_search_workers"),
    walks sibling `phase*/initial_program.py` and unions every
    `PHASE_LOCKED[WORKERS_KEY]`. Output schema includes `by_workers`.
  - Otherwise (z3): single measurement, flat schema.

Each (sha, W) pair runs `evaluation.repeats` times (10 by default) and is
averaged via `_lib.averaging.average_runs`, matching the variant solve path
so dtime ratios stay calibrated.

Stage3 skip: when W=1 (cpsat-style phase1/2), stage3 outliers can take
hours at low parallelism with no signal value (evaluator skips stage3 for
W=1 phases anyway). Drop stage3 SHAs from the W=1 task list.
"""
import argparse
import importlib.util
import json
import pathlib
import queue as _queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from _lib import averaging, bench_paths, params_catalog, runtime, subprocess_runner

REBASELINE_TIMEOUT_S = 3600
DEFAULT_REPEATS = 10


def _discover_phase_workers(bench_root, workers_key):
    if not workers_key:
        return [1]
    workers = set()
    for prog in sorted(pathlib.Path(bench_root).glob("phase*_*/initial_program.py")):
        try:
            spec = importlib.util.spec_from_file_location(prog.parent.name, prog)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"WARN: failed to load {prog.name}: {e}", file=sys.stderr)
            continue
        pl = getattr(mod, "PHASE_LOCKED", None)
        if isinstance(pl, dict) and workers_key in pl:
            try:
                workers.add(int(pl[workers_key]))
            except (TypeError, ValueError):
                pass
    return sorted(workers) if workers else [1]


def _parse_workers_arg(s):
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        w = int(tok)
        if w < 1:
            raise SystemExit(f"--workers: {w} must be >= 1")
        if w not in out:
            out.append(w)
    if not out:
        raise SystemExit("--workers: empty list")
    return sorted(out)


def _load_problem_index(adapter, problems_jsonl):
    idx = {}
    with open(problems_jsonl) as f:
        for line in f:
            d = json.loads(line)
            sha = d["problem_sha256"]
            status = d.get(adapter.STATUS_FIELD) or {}
            idx[sha] = {
                "sha": sha,
                "input_file": d[adapter.PROBLEM_FILE_FIELD],
                "raw_ms": status.get("elapsed_ms", 0),
                "raw_result": status.get("result"),
            }
    return idx


def _load_target_shas(cache_dir, include_stage3=True):
    samples = ["stage1_sample.json", "stage2_sample.json", "stage4_sample.json"]
    if include_stage3:
        samples.insert(2, "stage3_sample.json")
    ids = []
    seen = set()
    for fname in samples:
        path = cache_dir / fname
        if not path.exists():
            print(f"WARN: {fname} missing — skipping", file=sys.stderr)
            continue
        for sha in json.loads(path.read_text())["sha256"]:
            if sha not in seen:
                ids.append(sha)
                seen.add(sha)
    return ids


def _solve_one(worker_path, input_path, params, cores_block, repeats):
    runs = []
    for _ in range(repeats):
        r = subprocess_runner.run_solver(
            worker_path=worker_path,
            problem_path=input_path,
            params=params,
            timeout_s=REBASELINE_TIMEOUT_S,
            cpu_core=cores_block,
        )
        runs.append(r)
        if "invalid_param" in r:
            break
    return averaging.average_runs(runs)


def _measure_at_workers(tasks, worker_count, cores, worker_path, baseline_params, workers_key, repeats):
    if not tasks:
        print(f"  W={worker_count}: no tasks", flush=True)
        return []

    if workers_key:
        blocks = runtime.alloc_core_blocks(cores, worker_count)
        if not blocks:
            blocks = [list(cores)] if cores else [None]
    else:
        blocks = [[c] for c in cores] if cores else [None]
    n_parallel = min(len(blocks), len(tasks))
    blocks = blocks[:n_parallel]

    pool = _queue.Queue()
    for b in blocks:
        pool.put(b)

    params = dict(baseline_params)
    if workers_key:
        params[workers_key] = worker_count

    def _fmt(b):
        if isinstance(b, (list, tuple)):
            return ",".join(str(x) for x in b) if b else "-"
        return str(b) if b is not None else "-"

    print(f"  W={worker_count}: parallel={n_parallel} repeats={repeats} "
          f"blocks={[_fmt(b) for b in blocks]}", flush=True)

    def _task(t):
        i, meta, path = t
        block = pool.get()
        try:
            res = _solve_one(worker_path, path, params, block, repeats)
        finally:
            pool.put(block)
        return i, meta, res, block

    out = []
    if n_parallel == 1:
        for t in tasks:
            out.append(_task(t))
    else:
        with ThreadPoolExecutor(max_workers=n_parallel) as ex:
            futures = [ex.submit(_task, t) for t in tasks]
            for fut in as_completed(futures):
                out.append(fut.result())
    out.sort(key=lambda x: x[0])
    return out


def rebaseline(bench_root, workers_override=None):
    bench_root = pathlib.Path(bench_root).resolve()
    adapter = bench_paths.load_adapter(bench_root)
    catalog = params_catalog.load_for_bench(bench_root)
    cache = bench_paths.cache_dir(bench_root)
    cache.mkdir(parents=True, exist_ok=True)
    raw_dir = bench_paths.raw_dir(bench_root)
    problems_jsonl = bench_paths.problems_jsonl(bench_root)
    worker = bench_paths.worker_path(bench_root)

    eval_cfg = bench_paths.evaluation_cfg(bench_root)
    repeats = int(eval_cfg.get("repeats", DEFAULT_REPEATS))

    workers_key = getattr(adapter, "WORKERS_KEY", None)
    if workers_override is not None:
        worker_counts = _parse_workers_arg(workers_override)
    else:
        worker_counts = _discover_phase_workers(bench_root, workers_key)
    print(f"[rebaseline] worker counts: {worker_counts}  "
          f"(workers_key={workers_key!r})")

    baseline = dict(catalog.defaults)
    print(f"[rebaseline] BASELINE keys: {sorted(baseline.keys())}")

    idx = _load_problem_index(adapter, problems_jsonl)

    def _build_tasks(shas):
        out = []
        for i, sha in enumerate(shas):
            meta = idx.get(sha)
            if meta is None:
                raise SystemExit(f"{sha[:12]} missing from problems.jsonl")
            path = raw_dir / meta["input_file"]
            if not path.exists():
                raise SystemExit(f"input missing: {path}")
            out.append((i, meta, path))
        return out

    shas_full = _load_target_shas(cache, include_stage3=True)
    shas_no_s3 = _load_target_shas(cache, include_stage3=False)
    tasks_full = _build_tasks(shas_full)
    tasks_no_s3 = _build_tasks(shas_no_s3)

    cores = runtime.core_range()
    if cores is None:
        cores = list(range(1, runtime.parallel_solvers(
            bench_paths.config_path(bench_root), default=1) + 1))

    print(f"  {len(tasks_full)} total / {len(tasks_no_s3)} non-stage3")
    print(f"  per-problem timeout: {REBASELINE_TIMEOUT_S}s, cores={cores}")

    results = {}
    for meta in (m for _, m, _ in tasks_full):
        results[meta["sha"]] = {
            "raw_result": meta["raw_result"],
            "raw_elapsed_ms": meta["raw_ms"],
            "by_workers": {},
        }

    t0 = time.monotonic()
    mismatches = 0
    for w in worker_counts:
        tasks = tasks_no_s3 if (workers_key and w == 1) else tasks_full
        completed = _measure_at_workers(
            tasks, w, cores, worker, baseline, workers_key, repeats)
        for i, meta, res, block in completed:
            got_result = res.get("result", "Unknown")
            got_ms = int(res.get("elapsed_ms", 0))
            invalid = res.get("invalid_param")
            ok = (got_result == meta["raw_result"]) and not invalid
            if not ok:
                mismatches += 1
            ratio = got_ms / max(meta["raw_ms"], 1)
            block_str = (",".join(str(x) for x in block)
                         if isinstance(block, (list, tuple)) else str(block))
            flag = f"  INVALID={invalid}" if invalid else ("" if ok else "  MISMATCH")
            print(
                f"  [W={w} {i+1:>2}/{len(tasks)}] {meta['sha'][:10]}  "
                f"raw={meta['raw_result']!s:<10}/{int(meta['raw_ms']):>7}ms  "
                f"local={got_result!s:<10}/{got_ms:>7}ms  ratio={ratio:.2f}x{flag}  "
                f"cores={block_str}", flush=True
            )
            entry = {
                "elapsed_ms": got_ms,
                "result": got_result,
                "matches_raw": ok,
                "stats": res.get("stats") or {},
            }
            if "objective" in res:
                entry["objective"] = res["objective"]
            results[meta["sha"]]["by_workers"][str(w)] = entry

    out_path = cache / "local_baseline.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n")
    n_runs = sum(len(r["by_workers"]) for r in results.values())
    print()
    print(f"wrote {out_path.relative_to(bench_root.parent.parent)} "
          f"({len(results)} entries, {n_runs} (sha,W) runs, "
          f"{mismatches} mismatches)")
    print(f"total: {time.monotonic() - t0:.1f}s")
    return 0 if mismatches == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bench", help="bench dir name (e.g. cpsat-bench)")
    ap.add_argument("--workers", type=str, default=None,
                    help="comma-separated worker counts (e.g. '1,8')")
    args = ap.parse_args(argv)
    rc = rebaseline(bench_paths.resolve_bench(args.bench),
                    workers_override=args.workers)
    sys.exit(rc)


if __name__ == "__main__":
    main()
