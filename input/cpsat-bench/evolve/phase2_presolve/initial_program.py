"""
Phase 2: tune CP-SAT presolve / probing / symmetry knobs.

Targeted namespace: cp_model_probing_level, cp_model_presolve, presolve_use_bva,
presolve_bve_threshold, presolve_inclusion_work_limit,
merge_at_most_one_work_limit, probing_num_combinations_limit, symmetry_level.

Inherits phase1 winners via get_params() chaining is NOT used — phase1
GLOBAL_OVERRIDES live in cache/phase1_best.json (consumed by phase4_unified's
prepare step). Phase 2 evaluates presolve in isolation against BASELINE.

Do NOT modify locked keys. W=1 (single worker) for clean signal.
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
