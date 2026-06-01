"""
Cluster `problems.jsonl` into stage sample files.

CLI: `python -m _lib.sampler <bench>` (bench dir name under input/).

Reads `<bench>/evolve/config.yaml`'s `bench.clustering` section:

  clustering:
    method: kmeans               # kmeans | quintile | thresholds
    feature: features.num_constraints   # dotted path into problem record
    n_clusters: 5                # for kmeans / quintile
    thresholds: [50000, 150000]  # for method=thresholds (n_clusters = len + 1)
    max_baseline_ms: 120000      # optional pool cap (drop baselines > this)
    stage_sizes:                 # how many problems each stage keeps
      stage1: 10
      stage2: 10
      stage3: 5
      stage4: 20
    stage_clusters:              # which cluster IDs (ascending centroid) per stage
      stage1: [0, 1]
      stage2: [2, 3]
      stage3: [4]
      stage4: [0, 1, 2, 3, 4]
    spread: quintile             # quintile | center — how to pick within a stage pool

Outputs (under `<bench>/evolve/cache/`):
  stage{1..4}_sample.json   {"selection": "...", "sha256": [...], "summary": [...]}

The decisive-baseline filter is driven by the adapter
(`DECISIVE_RESULTS` + `STATUS_FIELD`).
"""
import argparse
import json
import pathlib
import sys

from _lib import bench_paths


def _dotted(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _kmeans_1d(values, k, max_iter=100):
    """1D Lloyd's k-means. Returns label list aligned with values, ordered by
    ascending centroid (label 0 = lowest cluster)."""
    n = len(values)
    if n == 0 or k <= 0:
        return []
    if n <= k:
        return list(range(n))  # each point its own cluster (sorted ascending)

    sorted_pairs = sorted(enumerate(values), key=lambda iv: iv[1])
    sorted_idx = [i for i, _ in sorted_pairs]
    sorted_vals = [v for _, v in sorted_pairs]
    centroids = [sorted_vals[((2 * c + 1) * n) // (2 * k)] for c in range(k)]
    labels = [0] * n

    for _ in range(max_iter):
        changed = False
        for i, v in enumerate(sorted_vals):
            best, best_d = 0, abs(v - centroids[0])
            for c in range(1, k):
                d = abs(v - centroids[c])
                if d < best_d:
                    best_d, best = d, c
            if labels[i] != best:
                labels[i] = best
                changed = True
        if not changed:
            break
        sums = [0.0] * k
        counts = [0] * k
        for v, lbl in zip(sorted_vals, labels):
            sums[lbl] += v
            counts[lbl] += 1
        for c in range(k):
            if counts[c] > 0:
                centroids[c] = sums[c] / counts[c]

    # Relabel so 0 = smallest centroid.
    order = sorted(range(k), key=lambda c: centroids[c])
    rank = {old: new for new, old in enumerate(order)}
    out = [0] * n
    for sorted_i, lbl in enumerate(labels):
        orig_i = sorted_idx[sorted_i]
        out[orig_i] = rank[lbl]
    return out


def _quintile_labels(values, n_buckets):
    """Assign each value to a bucket by rank (asc). Equal-size buckets."""
    n = len(values)
    if n == 0:
        return []
    sorted_pairs = sorted(enumerate(values), key=lambda iv: iv[1])
    labels = [0] * n
    for rank, (orig_i, _) in enumerate(sorted_pairs):
        b = (rank * n_buckets) // n
        if b >= n_buckets:
            b = n_buckets - 1
        labels[orig_i] = b
    return labels


def _threshold_labels(values, thresholds):
    """Bucket index = first threshold the value is < (last bucket = >= all)."""
    out = []
    for v in values:
        b = len(thresholds)
        for i, t in enumerate(thresholds):
            if v < t:
                b = i
                break
        out.append(b)
    return out


def _pick_spread(pool, n_pick, mode):
    if not pool or n_pick <= 0:
        return []
    if len(pool) <= n_pick:
        return list(pool)
    if mode == "center":
        start = (len(pool) - n_pick) // 2
        return pool[start:start + n_pick]
    # default: quintile spread within the pool
    if n_pick == 1:
        return [pool[len(pool) // 2]]
    out = []
    seen = set()
    for j in range(n_pick):
        idx = round(j * (len(pool) - 1) / (n_pick - 1))
        if idx not in seen:
            seen.add(idx)
            out.append(pool[idx])
    return out


def build_samples(bench_root, *, adapter=None):
    """Sample-build entry. `bench_root` is the absolute path to
    `<bench>/evolve/`."""
    bench_root = pathlib.Path(bench_root).resolve()
    cfg = bench_paths.load_config(bench_root)
    cluster_cfg = (cfg.get("bench") or {}).get("clustering") or {}
    if not cluster_cfg:
        raise SystemExit("bench.clustering missing from config.yaml")

    if adapter is None:
        adapter = bench_paths.load_adapter(bench_root)

    problems_jsonl = bench_root.parent / "problems.jsonl"
    if not problems_jsonl.exists():
        raise SystemExit(f"missing {problems_jsonl}")

    cache_dir = bench_paths.cache_dir(bench_root)
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    with open(problems_jsonl) as f:
        for line in f:
            rows.append(json.loads(line))
    print(f"scanned {len(rows)} problems from {problems_jsonl.name}")

    status_field = adapter.STATUS_FIELD
    decisive = set(adapter.DECISIVE_RESULTS)

    def _baseline_ms(r):
        return ((r.get(status_field) or {}).get("elapsed_ms")) or 0

    def _baseline_result(r):
        return (r.get(status_field) or {}).get("result")

    max_ms = cluster_cfg.get("max_baseline_ms")
    pool = []
    skipped = 0
    for r in rows:
        if _baseline_result(r) not in decisive:
            skipped += 1
            continue
        if max_ms is not None and _baseline_ms(r) > max_ms:
            skipped += 1
            continue
        pool.append(r)
    print(f"decisive + within-budget pool: {len(pool)} (skipped {skipped})")
    if not pool:
        raise SystemExit("no decisive-baseline problems to sample")

    feature_path = cluster_cfg.get("feature", "features.num_constraints")
    feature_vals = [_dotted(r, feature_path) for r in pool]
    if any(v is None for v in feature_vals):
        missing = sum(1 for v in feature_vals if v is None)
        print(f"warning: {missing} problems missing {feature_path}; treating as 0",
              file=sys.stderr)
        feature_vals = [v if v is not None else 0 for v in feature_vals]
    feature_vals = [float(v) for v in feature_vals]

    method = cluster_cfg.get("method", "kmeans")
    if method == "kmeans":
        k = int(cluster_cfg.get("n_clusters", 5))
        labels = _kmeans_1d(feature_vals, k)
        n_buckets = k
    elif method == "quintile":
        k = int(cluster_cfg.get("n_clusters", 5))
        labels = _quintile_labels(feature_vals, k)
        n_buckets = k
    elif method == "thresholds":
        thr = list(cluster_cfg.get("thresholds") or [])
        if not thr:
            raise SystemExit("clustering.method=thresholds requires "
                             "non-empty `thresholds` list")
        labels = _threshold_labels(feature_vals, thr)
        n_buckets = len(thr) + 1
    else:
        raise SystemExit(f"unknown clustering.method: {method}")

    buckets = [[] for _ in range(n_buckets)]
    for r, lbl in zip(pool, labels):
        buckets[lbl].append(r)

    # Sort within bucket by runtime ascending — stable spread picks.
    for b in buckets:
        b.sort(key=_baseline_ms)

    def _range(b):
        if not b:
            return "empty"
        return (f"feat={int(_dotted(b[0], feature_path) or 0)}.."
                f"{int(_dotted(b[-1], feature_path) or 0)} "
                f"ms={int(_baseline_ms(b[0]))}..{int(_baseline_ms(b[-1]))}")
    print("buckets: " + " | ".join(
        f"c{i}({len(b)},{_range(b)})" for i, b in enumerate(buckets)
    ))

    stage_sizes = cluster_cfg.get("stage_sizes") or {}
    stage_clusters = cluster_cfg.get("stage_clusters") or {}
    spread = cluster_cfg.get("spread", "quintile")

    for stage_name in sorted(stage_sizes.keys()):
        n_pick = int(stage_sizes[stage_name])
        cluster_ids = stage_clusters.get(stage_name) or []
        merged = []
        for cid in cluster_ids:
            if 0 <= cid < n_buckets:
                merged.extend(buckets[cid])
        merged.sort(key=_baseline_ms)
        picks = _pick_spread(merged, n_pick, spread)
        sample_path = cache_dir / f"{stage_name}_sample.json"
        criteria = (f"{method}-clusters={cluster_ids} feature={feature_path} "
                    f"spread={spread}")
        sample_path.write_text(
            json.dumps({
                "selection": f"{len(picks)} from {len(merged)} candidates",
                "criteria": criteria,
                "source": str(problems_jsonl.relative_to(bench_root.parent.parent)),
                "sha256": [r["problem_sha256"] for r in picks],
                "summary": [{
                    "sha": r["problem_sha256"][:12],
                    "baseline_result": _baseline_result(r),
                    "baseline_ms": _baseline_ms(r),
                    feature_path: _dotted(r, feature_path),
                } for r in picks],
            }, indent=2) + "\n"
        )
        print(f"wrote {sample_path.name} ({len(picks)} from clusters "
              f"{cluster_ids})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bench", help="bench dir name under input/ (e.g. cpsat-bench)")
    args = ap.parse_args(argv)
    bench_root = bench_paths.resolve_bench(args.bench)
    build_samples(bench_root)


if __name__ == "__main__":
    main()
