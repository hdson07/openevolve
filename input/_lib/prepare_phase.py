"""
Materialize a unified phase's initial_program.py with the union of
prior-phase winners (from cache/phaseN_best.json + optional
phaseN_buckets.json + phaseN_stage3.json).

CLI: `python -m _lib.prepare_phase <bench>`

Reads `<bench>/evolve/config.yaml`:
  - `bench.phases[*].dir` → ordered phase list
  - `bench.unified_prepare_before_dir` (optional) → which phase is the
    unified target. Defaults to the last phase.
  - `bench.unified_dict_name` (optional) → name of the merged-overrides
    dict written into the EVOLVE-BLOCK (default "UNIFIED_OVERRIDES";
    cpsat uses "GLOBAL_OVERRIDES").

Rewrites only the EVOLVE-BLOCK section. If a bench ships bucket / stage3
extracts, the block also gets `SIZE_BUCKETS` + `STAGE3_OVERRIDES`.
"""
import argparse
import json
import pathlib
import pprint
import re
import sys

from _lib import bench_paths

_EVOLVE_BLOCK_RE = re.compile(
    r"(# EVOLVE-BLOCK-START\n).*?(# EVOLVE-BLOCK-END)",
    re.DOTALL,
)


def _load(shared, n):
    f = pathlib.Path(shared) / f"phase{n}_best.json"
    if not f.exists():
        print(f"missing: {f}", file=sys.stderr)
        sys.exit(1)
    return json.loads(f.read_text())


def _maybe_load(shared, name):
    f = pathlib.Path(shared) / name
    if not f.exists():
        return None
    return json.loads(f.read_text())


def _merge_buckets(prior_buckets_list):
    """Merge a list of bucket-lists (one per phase) into a single
    list[(upper, dict)], ordered by ascending upper (None == inf last).
    Phases later in the list win on key conflicts within the same bucket."""
    # Collect all unique upper bounds, preserving inf -> None.
    by_upper = {}
    order_seen = []
    for blist in prior_buckets_list:
        if not blist:
            continue
        for upper, override in blist:
            key = upper  # None for inf, numeric otherwise
            if key not in by_upper:
                by_upper[key] = {}
                order_seen.append(key)
            by_upper[key].update(override)

    def _sort_key(k):
        return float("inf") if k is None else k

    return [[k, by_upper[k]] for k in sorted(by_upper.keys(), key=_sort_key)]


def _format_buckets_literal(buckets):
    """Render bucket list as Python source. None upper -> float('inf')."""
    lines = ["["]
    for upper, override in buckets:
        upper_src = "float('inf')" if upper is None else repr(upper)
        ov_src = pprint.pformat(override, width=100, sort_dicts=True)
        lines.append(f"    ({upper_src}, {ov_src}),")
    lines.append("]")
    return "\n".join(lines)


def main_cli(argv=None):
    """CLI entry: `python -m _lib.prepare_phase <bench>`."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bench", help="bench dir name (e.g. cpsat-bench)")
    args = ap.parse_args(argv)

    root = bench_paths.resolve_bench(args.bench)
    shared = bench_paths.cache_dir(root)
    cfg = bench_paths.load_config(root)
    bench_cfg = cfg.get("bench") or {}
    phases = [ph["dir"] for ph in (bench_cfg.get("phases") or [])]
    if not phases:
        raise SystemExit("bench.phases missing in config.yaml")

    target = bench_cfg.get("unified_prepare_before_dir") or phases[-1]
    if target not in phases:
        raise SystemExit(f"unified_prepare_before_dir={target!r} not in "
                         f"bench.phases ({phases})")
    target_idx = phases.index(target)
    if target_idx == 0:
        raise SystemExit(f"unified target {target!r} has no prior phases")
    prior_phases = list(range(1, target_idx + 1))
    unified_file = root / target / "initial_program.py"
    dict_name = bench_cfg.get("unified_dict_name") or "UNIFIED_OVERRIDES"
    if not unified_file.exists():
        raise SystemExit(f"unified initial_program.py not found: {unified_file}")
    return main(root, shared, prior_phases, unified_file, dict_name=dict_name)


def main(root, shared, prior_phases, unified_file, dict_name="UNIFIED_OVERRIDES"):
    """Direct invocation (also used by main_cli)."""
    unified_file = pathlib.Path(unified_file)

    merged = {}
    phase_counts = {}
    prior_buckets = []
    merged_stage3 = {}
    phase_stage3_counts = {}

    for n in prior_phases:
        d = _load(shared, n)
        phase_counts[n] = len(d)
        merged.update(d)

        b = _maybe_load(shared, f"phase{n}_buckets.json")
        if b is not None:
            prior_buckets.append(b)

        s = _maybe_load(shared, f"phase{n}_stage3.json")
        if isinstance(s, dict):
            phase_stage3_counts[n] = len(s)
            merged_stage3.update(s)

    print("merged keys: " + " ".join(f"p{n}={c}" for n, c in phase_counts.items())
          + f" union={len(merged)}")

    has_extras = bool(prior_buckets) or bool(merged_stage3)

    src = unified_file.read_text()

    if has_extras:
        merged_buckets = _merge_buckets(prior_buckets)
        nonempty = sum(1 for _, d in merged_buckets if d)
        if phase_stage3_counts:
            print("stage3 keys: "
                  + " ".join(f"p{n}={c}" for n, c in phase_stage3_counts.items())
                  + f" union={len(merged_stage3)}")
        print(f"size buckets: {len(merged_buckets)} entries ({nonempty} non-empty)")

        dict_repr = pprint.pformat(merged, width=100, sort_dicts=True)
        buckets_repr = _format_buckets_literal(merged_buckets)
        stage3_repr = pprint.pformat(merged_stage3, width=100, sort_dicts=True)
        new_block_body = (
            "# Auto-generated by prepare_phase_unified.py from union of prior "
            "phase winners.\n"
            f"{dict_name} = {dict_repr}\n"
            f"SIZE_BUCKETS = {buckets_repr}\n"
            f"STAGE3_OVERRIDES = {stage3_repr}\n"
        )
    else:
        dict_repr = pprint.pformat(merged, width=100, sort_dicts=True)
        new_block_body = (
            "# Auto-generated by prepare_phase_unified.py from union of prior "
            "phase winners.\n"
            f"{dict_name} = {dict_repr}\n"
        )

    new_src, n_subs = _EVOLVE_BLOCK_RE.subn(
        lambda m: m.group(1) + new_block_body + m.group(2),
        src,
        count=1,
    )
    if n_subs != 1:
        print(f"EVOLVE-BLOCK markers not found in {unified_file}", file=sys.stderr)
        sys.exit(1)
    unified_file.write_text(new_src)
    suffix = " (+ SIZE_BUCKETS, STAGE3_OVERRIDES)" if has_extras else ""
    print(f"wrote {unified_file.relative_to(pathlib.Path(root))} "
          f"({len(merged)} keys in EVOLVE-BLOCK){suffix}")


if __name__ == "__main__":
    main_cli()
