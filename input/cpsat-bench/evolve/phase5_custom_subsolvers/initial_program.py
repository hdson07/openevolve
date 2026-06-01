"""
Phase 5: add CUSTOM SUBSOLVERS to the CP-SAT portfolio at W=8.

CRITICAL — do NOT tune top-level parameters here. Top-level params apply to
EVERY subsolver, including LNS workers. Stick to a single isolated extra
subsolver per technique so its effect is attributable and the rest of the
portfolio is untouched.

Inherits the unified phase4 winner via cache/phase{4}_best.json
(GLOBAL_OVERRIDES), cache/phase4_buckets.json (SIZE_BUCKETS), and
cache/phase4_stage3.json (STAGE3_OVERRIDES) as the immutable top-level
config. Phase 5 ONLY appends custom subsolvers on top.

EVOLVE-BLOCK surface:
  CUSTOM_SUBSOLVERS         — applied to every problem
  STAGE3_CUSTOM_SUBSOLVERS  — added ONLY for stage3 outliers

Spec dict:
  {
    "name": "unique_subsolver_name",
    "params": { ...SatParameters fields },
    "needs_objective": False,           # optional
    "min_constraints": 0,               # optional inclusive lower gate
    "max_constraints": None,            # optional exclusive upper gate
  }

Do NOT modify locked keys (random_seed, num_search_workers, interleave_search).
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


PHASE5_WORKERS = 8

PHASE_LOCKED = {
    "random_seed": 0,
    "num_search_workers": PHASE5_WORKERS,
    "interleave_search": True,
}


def _load_prev_dict(name):
    p = _CACHE / name
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _load_prev_buckets(name):
    p = _CACHE / name
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return [(float("inf") if u is None else u, override) for u, override in raw]


_PHASE4 = _load_prev_dict("phase4_best.json")
_PHASE4_BUCKETS = _load_prev_buckets("phase4_buckets.json")
_PHASE4_STAGE3 = _load_prev_dict("phase4_stage3.json")


# EVOLVE-BLOCK-START
CUSTOM_SUBSOLVERS = []
STAGE3_CUSTOM_SUBSOLVERS = []
# EVOLVE-BLOCK-END


def _eligible(spec, problem):
    size = int(problem.get("size") or 0) if problem else 0
    lo = spec.get("min_constraints")
    if lo is not None and size < lo:
        return False
    hi = spec.get("max_constraints")
    if hi is not None and size >= hi:
        return False
    return True


def _bucket_override(size):
    out = {}
    for buckets in (_PHASE4_BUCKETS or [],):
        for upper, override in buckets:
            if size < upper:
                out.update(override)
                break
    return out


def _collect_specs(problem, stage):
    specs = [s for s in CUSTOM_SUBSOLVERS if _eligible(s, problem)]
    if stage == "stage3" and problem is not None and problem.get("is_outlier"):
        specs += [s for s in STAGE3_CUSTOM_SUBSOLVERS if _eligible(s, problem)]
    return specs


def _apply_custom_subsolvers(p, specs):
    if not specs:
        return
    sub_entries = []
    new_names = []
    for spec in specs:
        entry = {"name": spec["name"]}
        entry.update(spec.get("params") or {})
        sub_entries.append(entry)
        new_names.append(spec["name"])
    p["subsolver_params"] = sub_entries
    existing = list(p.get("extra_subsolvers") or [])
    for name in new_names:
        if name not in existing:
            existing.append(name)
    p["extra_subsolvers"] = existing


def get_params(problem=None, stage=None):
    p = dict(BASELINE)
    p.update(_PHASE4)
    if problem is not None:
        p.update(_bucket_override(int(problem.get("size") or 0)))
        if stage == "stage3" and problem.get("is_outlier"):
            p.update(_PHASE4_STAGE3)
    _apply_custom_subsolvers(p, _collect_specs(problem, stage))
    p.update(PHASE_LOCKED)
    return p


def get_phase_overrides():
    """Phase 5 evolves subsolver list, not a flat overrides dict.
    Return empty so extract_best does the right (no-op) thing for downstream."""
    return {}