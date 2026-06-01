"""
z3-bench solver hooks. Consumed by every _lib module via
bench_paths.load_adapter().
"""

SOLVER_NAME = "z3"

PROBLEM_FILE_FIELD = "smt2_filename"
STATUS_FIELD = "z3_status"                  # {"result", "elapsed_ms"}
STATS_FIELD = None                          # baseline has no separate stats block
FEATURES_FIELD = "features"
OBJECTIVE_FIELD = None                      # SMT2/MaxSMT: no objective in baseline

DECISIVE_RESULTS = ("Sat", "Unsat")
DECIDED_RESULTS  = ("Sat", "Unsat")

KEY_STATS = ("decisions", "propagations", "conflicts", "mk clause")

STATS_WEIGHTS = {
    "conflicts": 2.0,
    "decisions": 1.5,
    "propagations": 0.5,
}

SCORE_MODE = "speedup"

WORKERS_KEY = None  # z3 single-threaded in this bench


def get_problem_size(features):
    """Z3 problems carry num_hard_constraints as the dominant size signal
    (mean ~106k hard / ~2k soft / ~33k vars across the workload)."""
    return int((features or {}).get("num_hard_constraints") or 0)
