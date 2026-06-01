"""
Entry point loaded by openevolve-run.py as the evaluator. Resolves bench
context from OPENEVOLVE_BENCH_ROOT (exported by input/run_phase.sh) and
delegates to _lib.evaluator_core.build_evaluators().
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_INPUT = _HERE.parent
if str(_INPUT) not in sys.path:
    sys.path.insert(0, str(_INPUT))

from _lib import bench_paths, evaluator_core  # noqa: E402

_bench = bench_paths.bench_root_from_env()
if _bench is None:
    raise RuntimeError(
        "OPENEVOLVE_BENCH_ROOT not set — _lib/evaluator_entry.py expects "
        "input/run_phase.sh to export it. Set OPENEVOLVE_BENCH_ROOT=<absolute "
        "path to <bench>/evolve/> to invoke openevolve-run.py directly."
    )

_evals = evaluator_core.build_evaluators(_bench)
evaluate_stage1 = _evals["evaluate_stage1"]
evaluate_stage2 = _evals["evaluate_stage2"]
evaluate_stage3 = _evals["evaluate_stage3"]
evaluate_stage4 = _evals["evaluate_stage4"]
evaluate = _evals["evaluate"]
