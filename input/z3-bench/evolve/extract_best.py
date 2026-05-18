"""
After phase N completes, extract get_phase_overrides() from its best_program.py
and write shared/phaseN_best.json. Phase N+1 auto-loads it.

Default source: openevolve_output/best/best_program.py (created on normal phase
exit). For interrupted runs, pass --from-checkpoints to scan all
openevolve_output/checkpoints/checkpoint_*/ and pick the program with the
highest combined_score (read from best_program_info.json).

Usage:
    python extract_best.py <phase_num: 1|2|3>
    python extract_best.py <phase_num: 1|2|3> --from-checkpoints
"""
import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SHARED = ROOT / "shared"

PHASE_DIRS = {
    1: "phase1_opt_sls",
    2: "phase2_sat",
    3: "phase3_smt",
}


def _pick_from_checkpoints(phase_dir):
    ckpt_root = phase_dir / "openevolve_output" / "checkpoints"
    ckpts = sorted(
        ckpt_root.glob("checkpoint_*"),
        key=lambda p: int(p.name.split("_")[1]) if p.name.split("_")[1].isdigit() else -1,
    )
    if not ckpts:
        print(f"no checkpoints found under {ckpt_root}", file=sys.stderr)
        sys.exit(1)

    best_py = None
    best_score = float("-inf")
    best_ck = None
    for ck in ckpts:
        info_path = ck / "best_program_info.json"
        prog_path = ck / "best_program.py"
        if not info_path.exists() or not prog_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text())
            score = float(info.get("metrics", {}).get("combined_score", float("-inf")))
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"warning: failed to read {info_path}: {e}", file=sys.stderr)
            continue
        if score > best_score:
            best_score = score
            best_py = prog_path
            best_ck = ck

    if best_py is None:
        print(f"no usable best_program.py in any checkpoint under {ckpt_root}", file=sys.stderr)
        sys.exit(1)

    print(f"[extract_best] from-checkpoints: picked {best_ck.name} "
          f"(combined_score={best_score:.4f})")
    return best_py


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", type=int, choices=[1, 2, 3], help="phase number")
    ap.add_argument(
        "--from-checkpoints",
        action="store_true",
        help="scan checkpoint_*/ dirs and pick highest combined_score "
             "(use for interrupted runs without best/ dir)",
    )
    args = ap.parse_args()

    n = args.phase
    phase_dir = ROOT / PHASE_DIRS[n]

    if args.from_checkpoints:
        best_py = _pick_from_checkpoints(phase_dir)
    else:
        best_py = phase_dir / "openevolve_output" / "best" / "best_program.py"
        if not best_py.exists():
            print(f"best_program.py not found: {best_py}", file=sys.stderr)
            print("run phase first (./run_phase.sh N) before extracting,", file=sys.stderr)
            print("or pass --from-checkpoints to use the latest checkpoint.", file=sys.stderr)
            sys.exit(1)

    sys.path.insert(0, str(SHARED))

    spec = importlib.util.spec_from_file_location(f"phase{n}_best", best_py)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"failed to load {best_py}: {e}", file=sys.stderr)
        sys.exit(1)

    if not hasattr(module, "get_phase_overrides"):
        print(f"{best_py} missing get_phase_overrides()", file=sys.stderr)
        sys.exit(1)

    overrides = module.get_phase_overrides()
    if not isinstance(overrides, dict):
        print(f"get_phase_overrides() returned {type(overrides).__name__}, expected dict", file=sys.stderr)
        sys.exit(1)

    out = SHARED / f"phase{n}_best.json"
    out.write_text(json.dumps(overrides, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out.relative_to(ROOT)} ({len(overrides)} keys)")


if __name__ == "__main__":
    main()
