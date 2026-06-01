"""
cpsat-bench solver hooks. Consumed by every _lib module via
bench_paths.load_adapter().
"""

SOLVER_NAME = "cpsat"

# Field paths inside problems.jsonl entries.
PROBLEM_FILE_FIELD = "problem_filename"
STATUS_FIELD = "cpsat_status"               # {"result", "elapsed_ms", "objective_value", ...}
STATS_FIELD = "cpsat_response_stats"        # solver counters
FEATURES_FIELD = "features"                 # nested {"num_variables", "num_constraints", ...}
OBJECTIVE_FIELD = "objective_value"         # path inside STATUS_FIELD

# Result categorization.
DECISIVE_RESULTS = ("OPTIMAL", "FEASIBLE")
DECIDED_RESULTS  = ("OPTIMAL", "FEASIBLE", "INFEASIBLE")

# Solver counters surfaced into metrics / artifacts.
KEY_STATS = ("num_branches", "num_conflicts", "num_booleans",
             "wall_time", "user_time", "deterministic_time")

# stats_weights for the efficiency factor (lifted from old cpsat score.py).
STATS_WEIGHTS = {
    "num_conflicts": 2.0,
    "num_branches": 1.5,
}

# Score mode for _lib.scorer.score().
SCORE_MODE = "cost"

# Worker-count knob this solver uses (None when not applicable).
WORKERS_KEY = "num_search_workers"


def get_problem_size(features):
    """Feature value used by the size-bucketing surface (SIZE_BUCKETS)."""
    return int((features or {}).get("num_constraints") or 0)
