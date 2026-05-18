"""
OpenEvolve evaluator for Z3 parameter tuning.

Cascade:
  stage1: 5 stratified problems (stage1_sample.json), per-problem 15s
  stage2: full 50 problems, per-problem 120s

Score: geomean(speedup) * solved_rate^2 * efficiency^OPENEVOLVE_STATS_WEIGHT.
       efficiency = geomean over {decisions, propagations, conflicts, mk clause}
       of (baseline_stat / variant_stat) across solved problems. Default
       OPENEVOLVE_STATS_WEIGHT=0 keeps prior behaviour. Per-problem solver
       stats (z3 Optimize.statistics) are always surfaced via metrics +
       artifacts so the LLM sees solver-internal cost beyond wall-clock.

Locked params (sat.random_seed / smt.random_seed / sls.random_seed / parallel.enable)
must not deviate from baseline_params.LOCKED — violation => combined_score=0.

Environment overrides:
  OPENEVOLVE_MAX_PROBLEMS      int — cap stage2 problem count
  OPENEVOLVE_STAGE1_TIMEOUT    int sec — per-problem timeout in stage1 (default 24)
  OPENEVOLVE_STAGE2_TIMEOUT    int sec — per-problem timeout in stage2 (default 120)
  OPENEVOLVE_PARALLEL_SOLVERS  int — SINGLE knob for total z3 concurrency
                               (default 1). Spawns N z3 worker subprocesses
                               concurrently per stage; each pinned to a
                               dedicated core via taskset (Linux). Capped at
                               len(problems). Config.yaml parallel_evaluations
                               is fixed at 1 so total concurrent z3 = this env
                               value (no parallel_evaluations × N blow-up).
                               Total RAM ≈ N × 4 GB worst-case.
  OPENEVOLVE_Z3_BIN            str — z3 binary path (default "z3")
"""
import importlib.util
import json
import os
import pathlib
import sys
import traceback

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from baseline_params import BASELINE, LOCKED  # noqa: E402
from score import score  # noqa: E402
from z3_runner import run_z3  # noqa: E402
from runtime import parallel_solvers  # noqa: E402

from openevolve.evaluation_result import EvaluationResult  # noqa: E402

_BENCH_DIR = _HERE.parent.parent          # input/z3-bench/
_RAW_DIR = _BENCH_DIR / "raw-data"
_PROBLEMS_JSONL = _BENCH_DIR / "problems.jsonl"
_STAGE1_SAMPLE = _HERE / "stage1_sample.json"
_LOCAL_BASELINE = _HERE / "local_baseline.json"

_PYTHON_BIN = os.environ.get("OPENEVOLVE_PYTHON_BIN")  # None -> sys.executable


def _load_program(path):
    spec = importlib.util.spec_from_file_location("program", path)
    module = importlib.util.module_from_spec(spec)
    # Phase initial_programs add shared/ to sys.path themselves.
    spec.loader.exec_module(module)
    return module


def _load_problems():
    # If rebaseline_local.py has been run, restrict stage2 to that SHA subset
    # (20 stratified). Reasons:
    #   - non-rebaselined problems would use raw_ms recorded on a different
    #     machine, skewing speedup
    #   - smaller stage2 = faster iterations
    # Local elapsed_ms used only when local result matches raw, else fall
    # back to raw_ms for that SHA to avoid speedup distortion from a bad
    # local run (timeout, mismatch).
    local = {}
    if _LOCAL_BASELINE.exists():
        local = json.loads(_LOCAL_BASELINE.read_text())

    rows = []
    with open(_PROBLEMS_JSONL) as f:
        for line in f:
            d = json.loads(line)
            sha = d["problem_sha256"]
            if local and sha not in local:
                continue
            baseline_ms = d["z3_status"]["elapsed_ms"]
            baseline_result = d["z3_status"]["result"]
            baseline_stats = {}
            lo = local.get(sha)
            if lo and lo.get("matches_raw"):
                baseline_ms = lo["elapsed_ms"]
                baseline_stats = lo.get("stats") or {}
            rows.append(
                {
                    "sha": sha,
                    "smt2": d["smt2_filename"],
                    "baseline_ms": baseline_ms,
                    "baseline_result": baseline_result,
                    "baseline_stats": baseline_stats,
                }
            )
    return rows


def _filter_stage1(problems):
    if not _STAGE1_SAMPLE.exists():
        return problems[:5]
    keep = set(json.loads(_STAGE1_SAMPLE.read_text())["sha256"])
    return [p for p in problems if p["sha"] in keep]


def _err_result(metrics_extra, artifacts):
    metrics = {
        "combined_score": 0.0,
        "geomean_speedup": 0.0,
        "solved_rate": 0.0,
        "regressions": 0,
        "solved": 0,
        "total": 0,
    }
    metrics.update(metrics_extra)
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def _evaluate(program_path, problems, per_problem_timeout_s, stage_name):
    try:
        program = _load_program(program_path)
    except Exception as e:
        return _err_result(
            {"error": f"program load failed: {e}"},
            {
                "error_type": type(e).__name__,
                "error_message": str(e),
                "full_traceback": traceback.format_exc()[-2000:],
                "stage": stage_name,
            },
        )

    if not hasattr(program, "get_params"):
        return _err_result(
            {"error": "missing get_params()"},
            {"suggestion": "initial_program.py must expose get_params() -> dict", "stage": stage_name},
        )

    try:
        params = program.get_params()
        if not isinstance(params, dict):
            raise TypeError(f"get_params() returned {type(params).__name__}, expected dict")
    except Exception as e:
        return _err_result(
            {"error": f"get_params() raised: {e}"},
            {
                "error_type": type(e).__name__,
                "error_message": str(e),
                "full_traceback": traceback.format_exc()[-2000:],
                "stage": stage_name,
            },
        )

    violations = {k: params.get(k) for k in LOCKED if params.get(k) != LOCKED[k]}
    if violations:
        return _err_result(
            {"error": "locked params violated"},
            {
                "locked_violated": violations,
                "locked_expected": LOCKED,
                "stage": stage_name,
                "suggestion": "Do not modify sat.random_seed, smt.random_seed, sls.random_seed, parallel.enable",
            },
        )

    if "OPENEVOLVE_MAX_PROBLEMS" in os.environ:
        problems = problems[: int(os.environ["OPENEVOLVE_MAX_PROBLEMS"])]

    # Verify smt2 files exist up front so parallel dispatch doesn't race.
    for p in problems:
        smt2_path = _RAW_DIR / p["smt2"]
        if not smt2_path.exists():
            return _err_result(
                {"error": f"smt2 not found: {p['smt2']}"},
                {"missing_file": str(smt2_path), "stage": stage_name},
            )

    # Parallel dispatch — `OPENEVOLVE_PARALLEL_SOLVERS` controls how many
    # z3 worker subprocesses run concurrently for the stage's problem list.
    # Worker count is capped at len(problems) (no point spawning idle threads).
    # Each task is pinned to core (i % N) via taskset (Linux) so concurrent
    # runs don't fight for the same physical core. Default 1 = sequential.
    n_parallel = min(parallel_solvers(default=1), len(problems))

    def _solve(idx_p):
        idx, p = idx_p
        smt2_path = _RAW_DIR / p["smt2"]
        core = (idx % n_parallel) if n_parallel > 1 else None
        r = run_z3(smt2_path, params, per_problem_timeout_s,
                   python_bin=_PYTHON_BIN, cpu_core=core)
        return idx, p, r

    def _is_regression(p, r):
        # Correctness regression: baseline gave a definitive answer (Sat/Unsat)
        # and variant disagrees (Unsat, Unknown, or timeout). Unknown baseline
        # is excluded — variant solving an Unknown is an improvement, not a
        # regression. invalid_param handled separately (param-level error).
        if "invalid_param" in r:
            return False
        return (
            p["baseline_result"] in ("Sat", "Unsat")
            and r.get("result") != p["baseline_result"]
        )

    def _invalid_err(r):
        return _err_result(
            {"error": f"invalid z3 param: {r['invalid_param']}"},
            {
                "invalid_param": r["invalid_param"],
                "stderr": r.get("stderr", "")[:2000],
                "stage": stage_name,
                "suggestion": "Remove or fix this key in get_params().",
            },
        )

    def _regression_err(p, r):
        return _err_result(
            {"error": f"result regression on {p['sha'][:10]}: "
                      f"baseline={p['baseline_result']} got={r.get('result')}"},
            {
                "result_mismatch": {
                    "sha": p["sha"][:12],
                    "baseline_result": p["baseline_result"],
                    "got_result": r.get("result"),
                    "elapsed_ms": r.get("elapsed_ms"),
                    "timeout": bool(r.get("timeout")),
                },
                "stage": stage_name,
                "suggestion": (
                    "Variant lost correctness on a problem baseline solved. "
                    "Revert params that disable preprocessing or relax completeness "
                    "(e.g. simplifier off, aggressive sls early-prune, restart "
                    "tuning that starves Sat search)."
                ),
            },
        )

    by_idx = {}
    abort = None  # ("invalid"|"regression", p, r) — first failure observed
    if n_parallel == 1:
        # Sequential: short-circuit immediately on invalid or regression.
        for pair in enumerate(problems):
            idx, p, r = _solve(pair)
            print(f"  [{stage_name}] {idx+1}/{len(problems)} {p['sha'][:10]} "
                  f"{r.get('result')} {r.get('elapsed_ms')}ms", flush=True)
            if "invalid_param" in r:
                return _invalid_err(r)
            if _is_regression(p, r):
                print(f"  [{stage_name}] regression — aborting remaining "
                      f"{len(problems) - idx - 1} problems", flush=True)
                return _regression_err(p, r)
            by_idx[idx] = (p, r)
    else:
        # Parallel: cancel pending futures on first failure. In-flight tasks
        # keep running until subprocess timeout (with __exit__ waits for them);
        # acceptable for stage1 (timeout ~24s).
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=n_parallel) as ex:
            futures = [ex.submit(_solve, pair) for pair in enumerate(problems)]
            for fut in as_completed(futures):
                if abort is not None:
                    continue
                idx, p, r = fut.result()
                core = idx % n_parallel
                print(f"  [{stage_name}] {idx+1}/{len(problems)} {p['sha'][:10]} "
                      f"{r.get('result')} {r.get('elapsed_ms')}ms "
                      f"(core={core})", flush=True)
                if "invalid_param" in r:
                    abort = ("invalid", p, r)
                elif _is_regression(p, r):
                    abort = ("regression", p, r)
                if abort is not None:
                    print(f"  [{stage_name}] {abort[0]} — cancelling pending "
                          f"problems (in-flight workers will drain)", flush=True)
                    for f in futures:
                        f.cancel()
                    continue
                by_idx[idx] = (p, r)
        if abort is not None:
            kind, p, r = abort
            return _invalid_err(r) if kind == "invalid" else _regression_err(p, r)

    # No failure — reassemble in original problem order.
    results = []
    for idx in range(len(problems)):
        p, r = by_idx[idx]
        results.append(
            {
                **p,
                "result": r["result"],
                "elapsed_ms": r["elapsed_ms"],
                "timeout": bool(r.get("timeout")),
                "stats": r.get("stats") or {},
            }
        )

    metrics = score(results)
    metrics["stage"] = stage_name

    # Surface a small fixed set of solver-internal counters that drive Z3's
    # search shape. LLM sees these via metrics + per-problem artifacts and can
    # reason about what a param tweak did beyond wall-clock (e.g. fewer
    # decisions/conflicts at same elapsed_ms = sturdier improvement).
    # `mk clause` chosen over `restarts` since z3 does not emit `restarts`
    # for the optimize / arith-heavy stack used here.
    _KEY_STATS = ("decisions", "propagations", "conflicts", "mk clause")
    for k in _KEY_STATS:
        metrics[f"total_{k}"] = float(sum(r["stats"].get(k, 0) for r in results))

    sample = [
        {
            "sha": r["sha"][:10],
            "base_result": r["baseline_result"],
            "got_result": r["result"],
            "base_ms": r["baseline_ms"],
            "ms": r["elapsed_ms"],
            "speedup": round(r["baseline_ms"] / max(r["elapsed_ms"], 1), 3),
            "timeout": r["timeout"],
            "stats": {k: r["stats"].get(k, 0) for k in _KEY_STATS if k in r["stats"]},
            "base_stats": {k: r["baseline_stats"].get(k, 0) for k in _KEY_STATS if k in r.get("baseline_stats", {})},
        }
        for r in results
    ]
    artifacts = {
        "stage": stage_name,
        "summary": (
            f"solved={metrics['solved']}/{metrics['total']} "
            f"regressions={metrics['regressions']} "
            f"geomean_speedup={metrics['geomean_speedup']:.3f} "
            f"efficiency={metrics.get('efficiency', 1.0):.3f} "
            f"score={metrics['combined_score']:.3f}"
        ),
        "per_problem": sample[:20],
    }
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def evaluate_stage1(program_path):
    # Default 24s per problem → 5 problems = 60-120s wall-clock budget
    # (60s lower bound on baseline-like variants, 120s upper bound on all-timeout).
    timeout = int(os.environ.get("OPENEVOLVE_STAGE1_TIMEOUT", "24"))
    problems = _filter_stage1(_load_problems())
    return _evaluate(program_path, problems, timeout, "stage1")


def evaluate_stage2(program_path):
    timeout = int(os.environ.get("OPENEVOLVE_STAGE2_TIMEOUT", "120"))
    problems = _load_problems()
    return _evaluate(program_path, problems, timeout, "stage2")


def evaluate(program_path):
    # Evolution uses stage1 (5 sampled problems) only — fast iteration loop.
    # Stage2 (20 problems) is reserved for final verification via
    # final_verify.py on the best-program, not the per-variant search loop.
    return evaluate_stage1(program_path)
