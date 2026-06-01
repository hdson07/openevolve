"""
Phase 3: tune smt.* (SMT core — theories, quantifier instantiation, arith).

Inherits phase1 (opt.*/sls.*) and phase2 (sat.*) winners via cache/.
parallel.* stays at baseline.

NOTE: smt.auto_config=True (default) can silently override other smt.* options.
Force False inside OVERRIDES if your evolved keys must stick.

Do NOT modify smt.random_seed (locked). Invalid keys → evaluator returns 0.
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
_PHASE2 = _load_prev("phase2_best.json")


# EVOLVE-BLOCK-START
OVERRIDES = {}
# EVOLVE-BLOCK-END


def get_params():
    p = dict(BASELINE)
    p.update(_PHASE1)
    p.update(_PHASE2)
    p.update(OVERRIDES)
    return p


def get_phase_overrides():
    return dict(OVERRIDES)