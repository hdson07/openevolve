"""
Phase 4: unified refinement at W=8.

EVOLVE-BLOCK is auto-materialized by `python -m _lib.prepare_phase cpsat-bench`
before this phase runs — pulling the union of phase{1,2,3}_best.json winners
into GLOBAL_OVERRIDES, merged SIZE_BUCKETS, and merged STAGE3_OVERRIDES. The
LLM then tunes all three surfaces jointly.

Do NOT modify locked keys (random_seed, num_search_workers).
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


PHASE_LOCKED = {
    "random_seed": 0,
    "num_search_workers": 8,
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