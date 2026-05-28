"""
Phase 5: add CUSTOM SUBSOLVERS to the CP-SAT portfolio.

Reference: shared/cpsat_params_reference.md
  (any SatParameters field may be set INSIDE a custom subsolver's `params`;
   invalid names surface as `invalid_param: subsolver_params.<field>` and
   zero the score.)

WHY a dedicated phase for this
------------------------------
CP-SAT solves a model with a PORTFOLIO of subsolvers, each with a slightly
different configuration (e.g. different linearization_level). You may add your
OWN subsolver with a specific configuration via `subsolver_params` (a named
SatParameters set) referenced from `extra_subsolvers`.

CRITICAL — do NOT tune top-level parameters here. Top-level params apply to
EVERY subsolver, including the LNS workers. A very expensive propagation
technique enabled top-level would also fire inside LNS workers and make LNS so
slow it becomes useless, unbalancing the whole portfolio. You can also create a
default subsolver incompatible with the model: e.g. an objective-requiring
config on an objective-less model makes CP-SAT drop most/all subsolvers from
the portfolio, leaving the solver inefficient or non-functional.

The safe pattern is a SINGLE extra subsolver carrying the expensive technique:
  - if the technique does NOT help → only that one worker is slow; the rest of
    the portfolio is untouched.
  - if it DOES help → that worker shares its solutions and variable bounds with
    the others, lifting whole-portfolio performance.

This is exactly the packing-problem case: a high-cost propagation can speed up
search dramatically when isolated to one worker, but would cripple search if
forced on everyone.

Inheritance
-----------
Inherits the phase4 unified winner (shared/phase4_best.json + phase4_buckets +
phase4_stage3) as the top-level configuration and does NOT modify it. Phase 5
ONLY appends custom subsolvers on top. num_search_workers is pinned to
PHASE5_WORKERS (8) so the added subsolvers actually get worker slots; portfolio
sharing requires interleave_search, which is locked True.

Evolve surface (EVOLVE-BLOCK)
-----------------------------
  CUSTOM_SUBSOLVERS         — applied to every problem
  STAGE3_CUSTOM_SUBSOLVERS  — added ONLY for stage3 outliers (long-tail / hard)

Each entry is a dict:
  {
    "name": "unique_subsolver_name",       # required, unique string
    "params": { ...SatParameters fields },  # the isolated configuration
    "needs_objective": False,               # optional; if True, skipped on
                                            #   objective-less models (avoids
                                            #   CP-SAT dropping the portfolio)
    "min_constraints": 0,                   # optional inclusive lower gate
    "max_constraints": None,                # optional exclusive upper gate
  }
Only put the expensive/experimental knob(s) in `params`. Keep it to one
technique per subsolver so its effect is attributable.

Do NOT modify locked keys (random_seed, num_search_workers, interleave_search).
"""
import json
import pathlib
import sys

_SHARED = pathlib.Path(__file__).resolve().parent.parent / "shared"
sys.path.insert(0, str(_SHARED))

from baseline_params import BASELINE  # noqa: E402


PHASE5_WORKERS = 8

PHASE_LOCKED = {
    "random_seed": 0,
    "num_search_workers": PHASE5_WORKERS,
    # Portfolio determinism + the cross-worker sharing that makes an isolated
    # custom subsolver pay off (it broadcasts solutions / variable bounds).
    "interleave_search": True,
}


def _load_prev_dict(name):
    p = _SHARED / name
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _load_prev_buckets(name):
    p = _SHARED / name
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return [(float("inf") if u is None else u, override) for u, override in raw]


def _load_objective_shas():
    """SHAs whose model carries an objective (build_samples cache). Used to skip
    objective-requiring custom subsolvers on feasibility-only models."""
    p = _SHARED / "has_objective_cache.json"
    if not p.exists():
        return None  # unknown → treat every problem as having an objective
    try:
        data = json.loads(p.read_text())
        return set(data.get("with_objective") or [])
    except (json.JSONDecodeError, OSError):
        return None


# Inherit the consolidated phase4 unified winner as the top-level config.
_PHASE4 = _load_prev_dict("phase4_best.json")
_PHASE4_BUCKETS = _load_prev_buckets("phase4_buckets.json")
_PHASE4_STAGE3 = _load_prev_dict("phase4_stage3.json")
_OBJECTIVE_SHAS = _load_objective_shas()


# EVOLVE-BLOCK-START
# Custom subsolvers added to the portfolio. Start empty; the LLM introduces
# one isolated expensive technique at a time. See module docstring for schema.
# Example (commented — DO NOT enable blindly):
#   {
#     "name": "max_lp_heavy",
#     "params": {"linearization_level": 2, "add_mir_cuts": True,
#                "max_num_cuts": 12000, "cut_level": 2},
#     "min_constraints": 50000,
#   },
CUSTOM_SUBSOLVERS = []

# Added on top of CUSTOM_SUBSOLVERS only for stage3 outliers.
STAGE3_CUSTOM_SUBSOLVERS = []
# EVOLVE-BLOCK-END


def _problem_has_objective(problem):
    if problem is None:
        return True
    if _OBJECTIVE_SHAS is None:
        return True
    sha = problem.get("sha")
    if sha is None:
        # fall back to the recorded baseline objective when sha is absent
        return problem.get("baseline_objective") is not None
    return sha in _OBJECTIVE_SHAS


def _eligible(spec, problem):
    if spec.get("needs_objective") and not _problem_has_objective(problem):
        return False
    nc = int(problem.get("num_constraints") or 0) if problem else 0
    lo = spec.get("min_constraints")
    if lo is not None and nc < lo:
        return False
    hi = spec.get("max_constraints")
    if hi is not None and nc >= hi:
        return False
    return True


def _bucket_override(num_constraints):
    out = {}
    for buckets in (_PHASE4_BUCKETS or [],):
        for upper, override in buckets:
            if num_constraints < upper:
                out.update(override)
                break
    return out


def _collect_specs(problem, stage):
    specs = [s for s in CUSTOM_SUBSOLVERS if _eligible(s, problem)]
    if stage == "stage3" and problem is not None and problem.get("is_outlier"):
        specs += [s for s in STAGE3_CUSTOM_SUBSOLVERS if _eligible(s, problem)]
    return specs


def _apply_custom_subsolvers(p, specs):
    """Append custom subsolvers to the portfolio WITHOUT touching top-level
    params. Each spec → one entry in subsolver_params + one name in
    extra_subsolvers (deduped against any inherited names)."""
    if not specs:
        return
    sub_entries = []
    new_names = []
    for spec in specs:
        name = spec["name"]
        entry = {"name": name}
        entry.update(spec.get("params") or {})
        sub_entries.append(entry)
        new_names.append(name)

    p["subsolver_params"] = sub_entries

    existing = list(p.get("extra_subsolvers") or [])
    for name in new_names:
        if name not in existing:
            existing.append(name)
    p["extra_subsolvers"] = existing


def get_params(problem=None, stage=None):
    p = dict(BASELINE)
    p.update(_PHASE4)  # inherited top-level config — left untouched below
    if problem is not None:
        p.update(_bucket_override(int(problem.get("num_constraints") or 0)))
        if stage == "stage3" and problem.get("is_outlier"):
            p.update(_PHASE4_STAGE3)

    _apply_custom_subsolvers(p, _collect_specs(problem, stage))

    p.update(PHASE_LOCKED)  # re-enforce phase lock last
    return p
