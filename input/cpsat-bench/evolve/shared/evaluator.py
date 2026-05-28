"""
OpenEvolve evaluator for cpsat-bench parameter tuning.

Score mode: cost (minimize CP-SAT objective; see score.py).

Per-problem timeout = max(MIN_TIMEOUT_S, ceil(baseline_ms * TIMEOUT_FACTOR / 1000)).
Locked params (see baseline_params.LOCKED) must not deviate — violation => combined_score=0.

Per-problem param resolution:
  If the program defines `get_params(problem=None, stage=None)`, the evaluator
  calls it once per problem with the problem dict (carries num_constraints,
  num_variables, is_outlier) and the active stage name ("stage1"…"stage4").
  This lets phases ship SIZE_BUCKETS (constraint-count conditional overrides)
  and STAGE3_OVERRIDES (outlier-only tuning) inside their EVOLVE-BLOCK.
  Programs that still expose `get_params()` (no args) keep working — the
  evaluator falls back to a single global call and applies it uniformly.

Worker count / PHASE_LOCKED must stay identical across all problems within
a phase. The evaluator reads them once via the no-arg path; per-problem
get_params() calls must NOT change locked keys or num_search_workers.

Environment overrides:
  OPENEVOLVE_MAX_PROBLEMS      cap stage problem count
  OPENEVOLVE_PARALLEL_SOLVERS  concurrent solver subprocesses (default 1)
  OPENEVOLVE_PYTHON_BIN        python for worker subprocess
"""
import importlib.util
import inspect
import json
import logging
import math
import os
import pathlib
import sys
import traceback

logger = logging.getLogger(__name__)

TIMEOUT_FACTOR = 1.3
MIN_TIMEOUT_S = 5

# small profile repeats each solve N times and averages (deterministic_time
# + wall + counters) to damp multi-worker run-to-run variance. large profile
# (single huge outlier) runs once — repeating a ~10 min solve 10× is wasteful.
# Override with OPENEVOLVE_SOLVE_REPEATS.
N_REPEATS_SMALL = 10

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from baseline_params import BASELINE, LOCKED  # noqa: E402
from score import score  # noqa: E402
from cpsat_runner import run_cpsat  # noqa: E402
from runtime import (  # noqa: E402
    parallel_solvers, core_range, alloc_core_blocks,
)

from openevolve.evaluation_result import EvaluationResult  # noqa: E402

_BENCH_DIR = _HERE.parent.parent
_RAW_DIR = _BENCH_DIR / "raw-data"
_PROBLEMS_JSONL = _BENCH_DIR / "problems.jsonl"
_OUTLIERS_JSON = _HERE / "outliers.json"
_LOCAL_BASELINE = _HERE / "local_baseline.json"


def _profile():
    """Active sample profile (OPENEVOLVE_PROFILE env). 'small' (default) reads
    legacy stage{N}_sample.json; 'large' reads stage{N}_large_sample.json,
    falling back to legacy if the large file is missing."""
    return (os.environ.get("OPENEVOLVE_PROFILE") or "small").strip().lower()


def _stage_sample(stage_num):
    profile = _profile()
    if profile != "small":
        suffixed = _HERE / f"stage{stage_num}_{profile}_sample.json"
        if suffixed.exists():
            return suffixed
    return _HERE / f"stage{stage_num}_sample.json"


_STAGE1_SAMPLE = _stage_sample(1)
_STAGE2_SAMPLE = _stage_sample(2)
_STAGE3_SAMPLE = _stage_sample(3)
_STAGE4_SAMPLE = _stage_sample(4)


def _load_outlier_shas():
    """Return set of SHAs flagged by outliers_top.csv (via build_samples.py
    writing shared/outliers.json). Empty if file missing."""
    if not _OUTLIERS_JSON.exists():
        return set()
    try:
        d = json.loads(_OUTLIERS_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return set(d.get("outliers") or {})

_PYTHON_BIN = os.environ.get("OPENEVOLVE_PYTHON_BIN")

# Raw per-problem results cache (small profile). {(program_path, stage): [recs]}
_SMALL_RESULTS_CACHE: dict = {}

_KEY_STATS = ("num_branches", "num_conflicts", "num_booleans",
              "wall_time", "user_time", "deterministic_time")
_DECISIVE = ("OPTIMAL", "FEASIBLE")


def _load_program(path):
    spec = importlib.util.spec_from_file_location("program", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _solve_repeats():
    """How many times to run each solve before averaging. small profile → 10
    (damps multi-worker variance), large → 1. OPENEVOLVE_SOLVE_REPEATS env
    overrides."""
    env = os.environ.get("OPENEVOLVE_SOLVE_REPEATS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return N_REPEATS_SMALL if _profile() == "small" else 1


def _average_runs(runs):
    """Collapse N run_cpsat dicts into one with mean elapsed_ms / stats /
    objective. Repeated runs of the same params are near-identical except for
    multi-worker timing jitter, so the mean is the stable estimate. A run that
    surfaced invalid_param short-circuits (config error, not a timing sample).
    result = modal label; timeout = any run timed out."""
    import collections
    import statistics

    if not runs:
        return {"result": "Unknown", "elapsed_ms": 0, "stats": {}}
    for r in runs:
        if "invalid_param" in r:
            return r
    if len(runs) == 1:
        return runs[0]

    results = [r.get("result") for r in runs]
    result = collections.Counter(results).most_common(1)[0][0]
    elapsed = statistics.mean(r.get("elapsed_ms", 0) for r in runs)
    timeout_any = any(r.get("timeout") for r in runs)

    stat_keys = set()
    for r in runs:
        stat_keys |= set((r.get("stats") or {}).keys())
    stats = {}
    for k in stat_keys:
        vals = [(r.get("stats") or {}).get(k) for r in runs]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            stats[k] = statistics.mean(vals)

    out = {
        "result": result,
        "elapsed_ms": int(elapsed),
        "timeout": timeout_any,
        "stats": stats,
        "n_repeats": len(runs),
    }
    objs = [r.get("objective") for r in runs if r.get("objective") is not None]
    if objs:
        out["objective"] = statistics.mean(objs)
    return out


def _pick_local_baseline(lo_entry, workers):
    """Select the right local baseline entry for `workers`.

    New schema: {"by_workers": {"1": {...}, "8": {...}}, ...}.
    Legacy schema (flat keys: elapsed_ms/stats/objective/matches_raw) is
    treated as workers=1 measurement.

    Returns the inner dict (with matches_raw/elapsed_ms/stats/objective)
    or None if no usable entry exists for that worker count.
    """
    if not isinstance(lo_entry, dict):
        return None
    bw = lo_entry.get("by_workers")
    if isinstance(bw, dict) and bw:
        key = str(int(workers))
        if key in bw:
            return bw[key]
        # No exact match. Return None so caller falls back to raw_ms; we
        # intentionally do NOT cross-substitute (e.g. W=8 → W=1) because the
        # whole point of by_workers is to keep speedup ratios honest.
        return None
    # Legacy flat schema — treat as W=1 only.
    if "elapsed_ms" in lo_entry and int(workers) == 1:
        return lo_entry
    return None


def _load_problems(workers=1):
    local = {}
    if _LOCAL_BASELINE.exists():
        local = json.loads(_LOCAL_BASELINE.read_text())
    outlier_shas = _load_outlier_shas()
    rows = []
    with open(_PROBLEMS_JSONL) as f:
        for line in f:
            d = json.loads(line)
            sha = d["problem_sha256"]
            baseline_ms = (d.get("cpsat_status") or {}).get("elapsed_ms", 0)
            baseline_result = (d.get("cpsat_status") or {}).get("result")
            baseline_stats = d.get("cpsat_response_stats") or {}
            baseline_objective = (d.get("cpsat_status") or {}).get("objective_value")
            features = d.get("features") or {}
            lo = _pick_local_baseline(local.get(sha), workers)
            if lo and lo.get("matches_raw"):
                baseline_ms = lo["elapsed_ms"]
                baseline_stats = lo.get("stats") or baseline_stats
                if lo.get("objective") is not None:
                    baseline_objective = lo["objective"]
            rows.append({
                "sha": sha,
                "input_file": d["problem_filename"],
                "baseline_ms": baseline_ms,
                "baseline_result": baseline_result,
                "baseline_stats": baseline_stats,
                "baseline_objective": baseline_objective,
                "num_variables": int(features.get("num_variables") or 0),
                "num_constraints": int(features.get("num_constraints") or 0),
                "num_bool": int(features.get("num_bool") or 0),
                "num_int": int(features.get("num_int") or 0),
                "is_outlier": sha in outlier_shas,
            })
    return rows


def _filter_stage1(problems):
    if not _STAGE1_SAMPLE.exists():
        return problems
    keep = set(json.loads(_STAGE1_SAMPLE.read_text())["sha256"])
    return [p for p in problems if p["sha"] in keep]


def _filter_stage2(problems):
    if not _STAGE2_SAMPLE.exists():
        return problems
    keep = set(json.loads(_STAGE2_SAMPLE.read_text())["sha256"])
    return [p for p in problems if p["sha"] in keep]


def _filter_stage4(problems):
    if not _STAGE4_SAMPLE.exists():
        return problems
    keep = set(json.loads(_STAGE4_SAMPLE.read_text())["sha256"])
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


def _supports_kwargs(fn, *kwargs):
    """Return True if `fn` accepts any of the named kwargs (problem/stage)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params_ = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params_.values()):
        return True
    return any(name in params_ for name in kwargs)


def _resolve_params(program_get_params, problem, stage_name):
    """Call program.get_params(problem=..., stage=...) when supported, else
    fall back to get_params() (legacy single-dict per phase). Returns dict."""
    kwargs = {}
    if _supports_kwargs(program_get_params, "problem"):
        kwargs["problem"] = problem
    if _supports_kwargs(program_get_params, "stage"):
        kwargs["stage"] = stage_name
    p = program_get_params(**kwargs) if kwargs else program_get_params()
    if not isinstance(p, dict):
        raise TypeError(
            f"get_params() returned {type(p).__name__}, expected dict")
    return p


def _evaluate(program_path, problems, stage_name):
    try:
        program = _load_program(program_path)
    except Exception as e:
        return _err_result(
            {"error": f"program load failed: {e}"},
            {"error_type": type(e).__name__, "error_message": str(e),
             "full_traceback": traceback.format_exc()[-2000:], "stage": stage_name},
        )

    if not hasattr(program, "get_params"):
        return _err_result(
            {"error": "missing get_params()"},
            {"suggestion": "initial_program.py must expose get_params() -> dict",
             "stage": stage_name},
        )

    # Resolve "global" params (no problem context) once — used for worker
    # count, core-block allocation, and PHASE_LOCKED enforcement. Per-problem
    # variation only affects non-locked knobs (see _solve below).
    try:
        params = _resolve_params(program.get_params, None, stage_name)
    except Exception as e:
        return _err_result(
            {"error": f"get_params() raised: {e}"},
            {"error_type": type(e).__name__, "error_message": str(e),
             "full_traceback": traceback.format_exc()[-2000:], "stage": stage_name},
        )

    # Per-phase lock (worker count varies by phase) overrides the global lock.
    phase_locked = getattr(program, "PHASE_LOCKED", None)
    locked = phase_locked if isinstance(phase_locked, dict) else LOCKED

    # DEBUG: surface the resolved global params delta vs BASELINE so the run
    # log shows exactly which knobs the LLM mutated this iteration.
    if logger.isEnabledFor(logging.DEBUG):
        global_delta = {
            k: params[k] for k in params
            if k not in locked and params.get(k) != BASELINE.get(k)
        }
        logger.debug(
            f"[{stage_name}] resolved_global_delta_vs_baseline={global_delta} "
            f"program={pathlib.Path(program_path).name} "
            f"phase_locked={dict(locked) if isinstance(locked, dict) else locked}"
        )
        for attr in ("GLOBAL_OVERRIDES", "SIZE_BUCKETS", "STAGE3_OVERRIDES"):
            val = getattr(program, attr, None)
            if val:
                logger.debug(f"[{stage_name}] {attr}={val}")

    violations = {k: params.get(k) for k in locked if params.get(k) != locked[k]}
    if violations:
        return _err_result(
            {"error": "locked params violated"},
            {"locked_violated": violations, "locked_expected": locked,
             "stage": stage_name,
             "suggestion": "Do not modify locked keys (see PHASE_LOCKED in "
                           "this phase's initial_program.py)."},
        )

    if "OPENEVOLVE_MAX_PROBLEMS" in os.environ:
        problems = problems[: int(os.environ["OPENEVOLVE_MAX_PROBLEMS"])]

    # Empty stage sample → pass-through. Score is set well above any sane
    # cascade_thresholds entry so downstream stages keep running. Use this to
    # debug a single stage in isolation by emptying the samples for the others.
    if not problems:
        pass_through = {
            "combined_score": 100.0,
            "geomean_speedup": 100.0,
            "geomean_wall_speedup": 100.0,
            "solved_rate": 1.0,
            "regressions": 0,
            "solved": 0,
            "comparable": 0,
            "total": 0,
            "uncomparable": 0,
            "efficiency": 1.0,
            "efficiency_pairs": 0,
            "stats_weight": 0.0,
            "dtime_used": 0,
            "dtime_fallback": 0,
            "stage": stage_name,
        }
        return EvaluationResult(
            metrics=pass_through,
            artifacts={
                "stage": stage_name,
                "summary": "empty sample — stage skipped (cascade pass-through, score=100)",
            },
        )

    for p in problems:
        input_path = _RAW_DIR / p["input_file"]
        if not input_path.exists():
            return _err_result(
                {"error": f"input not found: {p['input_file']}"},
                {"missing_file": str(input_path), "stage": stage_name},
            )

    import queue as _queue
    # Core pool: OPENEVOLVE_CORE_RANGE (e.g. "2-7") overrides; else
    # cores 1..parallel_solvers() (core 0 reserved for kernel housekeeping).
    #
    # When params["num_search_workers"] = W > 1, each variant solve needs W
    # cores. We floor-chunk the core list into W-sized blocks; concurrency =
    # number of blocks. Leftover cores at the tail are dropped to keep every
    # solve's CPU budget identical (comparable timings).
    _cores = core_range()
    if _cores is None:
        _cores = list(range(1, parallel_solvers(default=1) + 1))
    workers_per_solve = int(params.get("num_search_workers", 1) or 1)
    _blocks = alloc_core_blocks(_cores, workers_per_solve)
    if not _blocks:
        # Not enough cores for even one block at workers_per_solve. Fall back
        # to a single block of all available cores (still respect taskset pin).
        _blocks = [list(_cores)] if _cores else [None]
    n_parallel = min(len(_blocks), len(problems))
    _blocks = _blocks[:n_parallel]
    _core_pool = _queue.Queue()
    for _b in _blocks:
        _core_pool.put(_b)

    repeats = _solve_repeats()

    def _solve(idx_p):
        idx, p = idx_p
        input_path = _RAW_DIR / p["input_file"]
        timeout_s = max(MIN_TIMEOUT_S, math.ceil(p["baseline_ms"] * TIMEOUT_FACTOR / 1000))
        # Per-problem param resolution. Locked keys + num_search_workers MUST
        # match the global `params` resolved earlier — re-pin defensively.
        try:
            per_params = _resolve_params(program.get_params, p, stage_name)
        except Exception as e:
            return idx, p, {"result": "ERROR", "elapsed_ms": 0,
                            "invalid_param": f"get_params(problem,stage) raised: {e}"}, \
                   None, timeout_s
        for k, v in locked.items():
            per_params[k] = v
        if "num_search_workers" in params:
            per_params["num_search_workers"] = params["num_search_workers"]
        if logger.isEnabledFor(logging.DEBUG):
            per_delta = {k: per_params[k] for k in per_params
                         if per_params.get(k) != params.get(k)}
            if per_delta:
                logger.debug(
                    f"[{stage_name}] sha={p['sha'][:10]} "
                    f"num_constraints={p['num_constraints']} "
                    f"is_outlier={p['is_outlier']} "
                    f"per_problem_delta_vs_global={per_delta}"
                )
        core = _core_pool.get()
        try:
            runs = []
            for _ in range(repeats):
                rr = run_cpsat(input_path, per_params, timeout_s,
                               python_bin=_PYTHON_BIN, cpu_core=core)
                runs.append(rr)
                if "invalid_param" in rr:
                    break  # config error — no point repeating
            r = _average_runs(runs)
        finally:
            _core_pool.put(core)
        return idx, p, r, core, timeout_s

    def _invalid_err(r):
        return _err_result(
            {"error": f"invalid param: {r['invalid_param']}"},
            {"invalid_param": r["invalid_param"], "stderr": r.get("stderr", "")[:2000],
             "stage": stage_name,
             "suggestion": "Remove or fix this key in get_params()."},
        )

    def _fmt_core(c):
        if c is None:
            return "-"
        if isinstance(c, (list, tuple)):
            return ",".join(str(x) for x in c) if c else "-"
        return str(c)

    print(f"  [{stage_name}] workers/solve={workers_per_solve} "
          f"repeats={repeats} "
          f"core_blocks={[_fmt_core(b) for b in _blocks]}", flush=True)

    # Non-feasible variants are NO LONGER aborted/zeroed — the variant's real
    # measured time (≈ timeout when it ran the full budget) flows into the
    # score, so a regression is penalized by its true slowdown + solved_rate
    # drop, not by a 1e-6 sentinel. Only invalid_param (a config error, not a
    # timing sample) still aborts the whole evaluation.
    by_idx = {}
    abort = None
    if n_parallel == 1:
        for pair in enumerate(problems):
            idx, p, r, core, timeout_s = _solve(pair)
            print(f"  [{stage_name}] {idx+1}/{len(problems)} {p['sha'][:10]} "
                  f"{r.get('result')} {r.get('elapsed_ms')}ms / {timeout_s}s "
                  f"(core={_fmt_core(core)})", flush=True)
            if "invalid_param" in r:
                return _invalid_err(r)
            by_idx[idx] = (p, r)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ordered = sorted(enumerate(problems), key=lambda ip: -ip[1]["baseline_ms"])
        with ThreadPoolExecutor(max_workers=n_parallel) as ex:
            futures = [ex.submit(_solve, pair) for pair in ordered]
            for fut in as_completed(futures):
                if abort is not None:
                    continue
                idx, p, r, core, timeout_s = fut.result()
                print(f"  [{stage_name}] {idx+1}/{len(problems)} {p['sha'][:10]} "
                      f"{r.get('result')} {r.get('elapsed_ms')}ms / {timeout_s}s "
                      f"(core={_fmt_core(core)})", flush=True)
                if "invalid_param" in r:
                    abort = ("invalid", p, r)
                    print(f"  [{stage_name}] invalid_param — cancelling pending "
                          f"(in-flight workers will drain)", flush=True)
                    for f in futures:
                        f.cancel()
                    continue
                by_idx[idx] = (p, r)
        if abort is not None:
            kind, p, r = abort
            return _invalid_err(r)

    results = []
    for idx in range(len(problems)):
        p, r = by_idx[idx]
        rec = {
            **p,
            "result": r["result"],
            "elapsed_ms": r["elapsed_ms"],
            "timeout": bool(r.get("timeout")),
            "stats": r.get("stats") or {},
        }
        if "objective" in r:
            rec["objective"] = r["objective"]
        results.append(rec)

    # Cache raw per-problem results so the small cascade can merge stage1 +
    # stage2 into one combined final score (see evaluate_stage3). Keyed by
    # program_path + stage; overwritten each cascade so no stale leakage.
    _SMALL_RESULTS_CACHE[(str(program_path), stage_name)] = results

    metrics = score(results)
    metrics["stage"] = stage_name

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
            "base_obj": r.get("baseline_objective"),
            "obj": r.get("objective"),
            "stats": {k: r["stats"].get(k, 0) for k in _KEY_STATS if k in r["stats"]},
            "base_stats": {k: r["baseline_stats"].get(k, 0)
                           for k in _KEY_STATS if k in r.get("baseline_stats", {})},
        }
        for r in results
    ]
    artifacts = {
        "stage": stage_name,
        "summary": (
            f"solved={metrics['solved']}/{metrics['total']} "
            f"regressions={metrics['regressions']} "
            f"geomean_dtime={metrics['geomean_speedup']:.3f} "
            f"geomean_wall={metrics.get('geomean_wall_speedup', 0.0):.3f} "
            f"dtime_used={metrics.get('dtime_used', 0)}/"
            f"{metrics.get('dtime_used', 0) + metrics.get('dtime_fallback', 0)} "
            f"efficiency={metrics.get('efficiency', 1.0):.3f} "
            f"score={metrics['combined_score']:.3f}"
        ),
        "per_problem": sample[:20],
    }
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def _peek_workers(program_path):
    """Resolve num_search_workers for baseline lookup BEFORE running _evaluate.

    Prefer PHASE_LOCKED (authoritative); fall back to get_params(). On any
    failure, return 1 — _evaluate's own program-load handles the real error
    reporting later."""
    try:
        program = _load_program(program_path)
    except Exception:
        return 1
    pl = getattr(program, "PHASE_LOCKED", None)
    if isinstance(pl, dict) and "num_search_workers" in pl:
        try:
            return int(pl["num_search_workers"])
        except (TypeError, ValueError):
            pass
    try:
        params = program.get_params()
        return int(params.get("num_search_workers", 1) or 1)
    except Exception:
        return 1


# Large profile: no cascade stages — every cascade call evaluates the same
# outlier set once and caches. Cache lifetime = current evaluator process
# (openevolve spawns a fresh worker per variant, so cache holds exactly one
# entry per iteration). Keyed by program_path so a re-used path string doesn't
# leak stale metrics across variants.
_LARGE_RESULT_CACHE: dict = {}


def _load_large_sample_shas():
    path = _HERE / "stage1_large_sample.json"
    if not path.exists():
        return None
    try:
        return set(json.loads(path.read_text())["sha256"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _evaluate_large(program_path):
    """Single-stage evaluation for OPENEVOLVE_PROFILE=large. Runs all outliers
    once, caches result, returns same EvaluationResult on subsequent cascade
    calls (stage2/stage3 entry points) so the final variant score reflects the
    real outlier score instead of a pass-through placeholder."""
    key = str(program_path)
    cached = _LARGE_RESULT_CACHE.get(key)
    if cached is not None:
        return cached

    keep = _load_large_sample_shas()
    if keep is None:
        result = _err_result(
            {"error": "stage1_large_sample.json missing/invalid"},
            {"suggestion": "Run build_samples.py to regenerate.",
             "stage": "large"},
        )
        _LARGE_RESULT_CACHE[key] = result
        return result

    w = _peek_workers(program_path)
    problems = [p for p in _load_problems(w) if p["sha"] in keep]
    result = _evaluate(program_path, problems, "large")
    _LARGE_RESULT_CACHE[key] = result
    return result


def evaluate_stage1(program_path):
    if _profile() == "large":
        return _evaluate_large(program_path)
    w = _peek_workers(program_path)
    return _evaluate(program_path, _filter_stage1(_load_problems(w)), "stage1")


def evaluate_stage2(program_path):
    if _profile() == "large":
        return _evaluate_large(program_path)
    w = _peek_workers(program_path)
    return _evaluate(program_path, _filter_stage2(_load_problems(w)), "stage2")


def _finalize_small(program_path):
    """Small-profile final cascade slot. The cascade runs stage1 then stage2;
    this merges their cached per-problem results into ONE combined score over
    stage1 ∪ stage2. No stage3 (outliers live in the large profile) and no
    stage4. Falls back to re-running a stage if its cache entry is absent."""
    key = str(program_path)
    r1 = _SMALL_RESULTS_CACHE.get((key, "stage1"))
    if r1 is None:
        res = evaluate_stage1(program_path)
        if not isinstance(res, EvaluationResult):
            return res
        r1 = _SMALL_RESULTS_CACHE.get((key, "stage1"), [])
    r2 = _SMALL_RESULTS_CACHE.get((key, "stage2"))
    if r2 is None:
        res = evaluate_stage2(program_path)
        if not isinstance(res, EvaluationResult):
            return res
        r2 = _SMALL_RESULTS_CACHE.get((key, "stage2"), [])

    combined = list(r1) + list(r2)
    if not combined:
        return _evaluate(program_path, [], "small_final")  # empty pass-through

    metrics = score(combined)
    metrics["stage"] = "small_final"
    for k in _KEY_STATS:
        metrics[f"total_{k}"] = float(sum(r["stats"].get(k, 0) for r in combined))
    artifacts = {
        "stage": "small_final",
        "summary": (
            f"combined stage1+stage2 ({len(combined)} problems) "
            f"solved={metrics['solved']}/{metrics['total']} "
            f"regressions={metrics['regressions']} "
            f"geomean_dtime={metrics['geomean_speedup']:.3f} "
            f"score={metrics['combined_score']:.3f}"
        ),
    }
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def evaluate_stage3(program_path):
    if _profile() == "large":
        return _evaluate_large(program_path)
    # small profile: cascade is stage1 → stage2 only. This final slot merges
    # cached stage1 + stage2 results into one combined score. stage3 (outliers)
    # and stage4 (broad spread) are no longer part of the small cascade.
    return _finalize_small(program_path)


def evaluate_stage4(program_path):
    # Standalone entry for manual / final-verify use. Not invoked by cascade.
    if _profile() == "large":
        return _evaluate_large(program_path)
    w = _peek_workers(program_path)
    problems = _filter_stage4(_load_problems(w))
    return _evaluate(program_path, problems, "stage4")


def evaluate(program_path):
    # Evolution loop entry: stage1 only. Cascade chains 2/3/4.
    return evaluate_stage1(program_path)
