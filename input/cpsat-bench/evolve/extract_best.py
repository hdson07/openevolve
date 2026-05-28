"""
Thin wrapper: calls _lib.extract_best.main with cpsat-bench phase map.
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SHARED = _HERE / "shared"
_INPUT_DIR = _HERE.parents[1]

if str(_INPUT_DIR) not in sys.path:
    sys.path.insert(0, str(_INPUT_DIR))

from _lib.extract_best import main  # noqa: E402

PHASE_DIRS = {
    1: "phase1_search",
    2: "phase2_presolve",
    3: "phase3_lp_cuts",
    # phase4 is no longer terminal (phase5 follows it), so it gets extracted to
    # phase4_best.json / phase4_buckets.json / phase4_stage3.json for phase5 to
    # inherit. phase5 (custom subsolvers) is the terminal phase — no extract.
    4: "phase4_unified",
}

if __name__ == "__main__":
    main(_HERE, _SHARED, PHASE_DIRS)
