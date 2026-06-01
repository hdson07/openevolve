"""
Final verification: re-run an evolved best_program.py through stage4 of
the unified evaluator and print a summary.

CLI: `python -m _lib.final_verify <bench> <program_path>`

Assumes `cache/local_baseline.json` is current. Re-run
`python -m _lib.rebaseline <bench>` first if hardware / solver version
has changed since the evolution run.

Optional `cache/final_sample.json` (shape: {"sha256": [...]}) overrides
the stage4 sample; otherwise uses stage4_sample.json (same as the
cascade's final stage).
"""
import argparse
import json
import pathlib
import sys
import time

from _lib import bench_paths, evaluator_core


def _stash_stage4(cache_dir):
    """If final_sample.json exists, swap it in temporarily as stage4_sample.json.
    Returns (original_path_backup_or_None, did_swap)."""
    final = cache_dir / "final_sample.json"
    stage4 = cache_dir / "stage4_sample.json"
    if not final.exists():
        return None, False
    backup = stage4.with_suffix(".json.fv-backup")
    if stage4.exists():
        stage4.rename(backup)
    stage4.write_text(final.read_text())
    return backup, True


def _restore_stage4(cache_dir, backup, did_swap):
    if not did_swap:
        return
    stage4 = cache_dir / "stage4_sample.json"
    if stage4.exists():
        stage4.unlink()
    if backup and backup.exists():
        backup.rename(stage4)


def final_verify(bench_root, program_path):
    bench_root = pathlib.Path(bench_root).resolve()
    program_path = pathlib.Path(program_path).resolve()
    if not program_path.exists():
        raise SystemExit(f"program not found: {program_path}")

    cache = bench_paths.cache_dir(bench_root)
    backup, did_swap = _stash_stage4(cache)
    try:
        evals = evaluator_core.build_evaluators(bench_root)
        t0 = time.monotonic()
        result = evals["evaluate_stage4"](program_path)
        elapsed = time.monotonic() - t0
    finally:
        _restore_stage4(cache, backup, did_swap)

    metrics = getattr(result, "metrics", {})
    artifacts = getattr(result, "artifacts", {})
    print()
    print("===== final_verify summary =====")
    print(f"program:         {program_path}")
    print(f"bench:           {bench_root.parent.name}")
    print(f"wall:            {elapsed:.1f}s")
    print(f"combined_score:  {metrics.get('combined_score', 0.0):.4f}")
    print(f"geomean_speedup: {metrics.get('geomean_speedup', 0.0):.4f}")
    print(f"solved_rate:     {metrics.get('solved_rate', 0.0):.4f}")
    print(f"solved:          {metrics.get('solved', 0)}/{metrics.get('total', 0)}")
    print(f"regressions:     {metrics.get('regressions', 0)}")
    print(f"efficiency:      {metrics.get('efficiency', 1.0):.4f}")
    if "summary" in artifacts:
        print(f"summary:         {artifacts['summary']}")
    out = program_path.parent / "final_verify.json"
    out.write_text(json.dumps({
        "program": str(program_path),
        "bench": bench_root.parent.name,
        "metrics": metrics,
        "summary": artifacts.get("summary", ""),
    }, indent=2) + "\n")
    print(f"wrote {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bench", help="bench dir name (e.g. cpsat-bench)")
    ap.add_argument("program", help="path to best_program.py")
    args = ap.parse_args(argv)
    final_verify(bench_paths.resolve_bench(args.bench), args.program)


if __name__ == "__main__":
    main()
