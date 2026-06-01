"""
Phase 1: tune Z3 opt.* + sls.* knobs.

Other namespaces (sat.*, smt.*, parallel.*) stay at baseline. Z3 4.13.x keys.

Do NOT modify locked keys (sat.random_seed, smt.random_seed, sls.random_seed,
parallel.enable, threads). Invalid Z3 keys cause evaluator to return 0.
"""
import os
import pathlib
import sys

def _resolve_bench_root():
    v = os.environ.get("OPENEVOLVE_BENCH_ROOT")
    if v:
        return pathlib.Path(v).resolve()
    here = pathlib.Path(__file__).resolve()
    for p in [here.parent.parent] + list(here.parents):
        if (p / "params.json").exists() and (p / "adapter.py").exists():
            return p
    raise RuntimeError(
        "OPENEVOLVE_BENCH_ROOT unset and no adapter/params.json found "
        "walking up from " + str(here)
    )


_BENCH = _resolve_bench_root()
_INPUT = _BENCH.parent.parent
if str(_INPUT) not in sys.path:
    sys.path.insert(0, str(_INPUT))

from _lib import params_catalog  # noqa: E402

BASELINE = params_catalog.load_for_bench(_BENCH).defaults


# EVOLVE-BLOCK-START
OVERRIDES = {}
# EVOLVE-BLOCK-END


def get_params():
    p = dict(BASELINE)
    p.update(OVERRIDES)
    return p


def get_phase_overrides():
    return dict(OVERRIDES)