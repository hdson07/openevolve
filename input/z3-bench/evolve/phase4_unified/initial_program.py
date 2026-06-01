"""
Phase 4: unified Z3 refinement.

EVOLVE-BLOCK is auto-materialized by `python -m _lib.prepare_phase z3-bench`
before this phase runs — pulling the union of phase{1,2,3}_best.json winners
into UNIFIED_OVERRIDES. The LLM then tunes the merged dict.

Do NOT modify locked keys.
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
UNIFIED_OVERRIDES = {}
# EVOLVE-BLOCK-END


def get_params():
    p = dict(BASELINE)
    p.update(UNIFIED_OVERRIDES)
    return p


def get_phase_overrides():
    return dict(UNIFIED_OVERRIDES)