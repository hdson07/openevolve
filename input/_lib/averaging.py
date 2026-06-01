"""
Collapse N repeated `run_solver` dicts into one mean dict. Lifted verbatim
from cpsat-bench's `_average_runs` so both benches now share the same
averaging semantics:

  - invalid_param short-circuits (config error, not a timing sample)
  - result = modal label across runs
  - elapsed_ms = mean across runs
  - timeout = any run timed out
  - stats[k] = mean over runs that emitted numeric k
  - objective = mean over runs that emitted objective
  - n_repeats annotated so the evaluator log can show repeats=N
"""
import collections
import statistics


def average_runs(runs):
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
        vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
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
