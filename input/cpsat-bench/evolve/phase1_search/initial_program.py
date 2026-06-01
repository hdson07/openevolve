"""
Phase 1: tune CP-SAT search / subsolver knobs.

Targeted namespace: extra_subsolvers, ignore_subsolvers, interleave_search,
use_feasibility_jump, use_feasibility_pump, search_branching,
preferred_variable_order, repair_hint, diversify_lns_params.

Other params stay at BASELINE. Phase 1 pins num_search_workers=1 so other
knobs are evaluated without multi-thread noise. Phase 3 raises the worker
count to explore subsolver-mix effects.

Do NOT modify locked keys (random_seed, num_search_workers).
Invalid solver keys cause evaluator to return 0 and surface the offending key.
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


# OPENEVOLVE_PROFILE=large → W=8 (outlier tuning track); default small → W=1.
_LARGE_PROFILE = (os.environ.get("OPENEVOLVE_PROFILE", "small").strip().lower()
                  == "large")
PHASE_LOCKED = {
    "random_seed": 0,
    "num_search_workers": 8 if _LARGE_PROFILE else 1,
}


# EVOLVE-BLOCK-START
GLOBAL_OVERRIDES = {}
SIZE_BUCKETS = [
    (50_000,         {}),
    (150_000,        {}),
    (float("inf"),   {}),
]
STAGE3_OVERRIDES = {}
# EVOLVE-BLOCK-END


def _bucket_override(size):
    for upper, override in SIZE_BUCKETS:
        if size < upper:
            return override
    return {}


def get_params(problem=None, stage=None):
    p = dict(BASELINE)
    p.update(GLOBAL_OVERRIDES)
    if problem is not None:
        p.update(_bucket_override(int(problem.get("size") or 0)))
        if stage == "stage3" and problem.get("is_outlier"):
            p.update(STAGE3_OVERRIDES)
    p.update(PHASE_LOCKED)
    return p


def get_phase_overrides():
    return dict(GLOBAL_OVERRIDES)


def get_phase_size_buckets():
    return [(u, dict(d)) for u, d in SIZE_BUCKETS]


def get_phase_stage3_overrides():
    return dict(STAGE3_OVERRIDES)
