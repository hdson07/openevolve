"""
Phase 2: tune sat.* (CDCL SAT core).

Inherits phase1 (opt.*+sls.*) winners via cache/phase1_best.json. smt.* and
parallel.* stay at baseline. EVOLVE-BLOCK is OVERRIDES below.

Do NOT modify sat.random_seed (locked). Invalid keys → evaluator returns 0.
"""
import json
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
_CACHE = _BENCH / "cache"


def _load_prev(name):
    p = _CACHE / name
    return json.loads(p.read_text()) if p.exists() else {}


_PHASE1 = _load_prev("phase1_best.json")


# EVOLVE-BLOCK-START
OVERRIDES = {}
# EVOLVE-BLOCK-END


def get_params():
    p = dict(BASELINE)
    p.update(_PHASE1)
    p.update(OVERRIDES)
    return p


def get_phase_overrides():
    return dict(OVERRIDES)