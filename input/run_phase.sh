#!/bin/bash
# Single entry for all bench phase runs. Reads `bench:` section of
# <bench>/evolve/config.yaml (via _lib/load_bench_config.py), then drives the
# refactored flow — everything routes through `python -m _lib.<module>`,
# there are no per-bench wrapper scripts anymore.
#
# Usage:
#   ./input/run_phase.sh <bench> [<phase>] [--pin N-M] [--extract-only]
#                                          [--iterations <N>] [--profile small|large]
#                                          [extra openevolve flags]
#
#   <phase> omitted  → run ALL phases sequentially (1..N).
#   <phase> numeric  → run that single phase.
#
# Pre-flight (run once before the phase loop):
#   - if cache/stage1_sample.json missing → `python -m _lib.sampler <bench>`
#   - if cache/local_baseline.json missing → `python -m _lib.rebaseline <bench>`
#     (SKIP_REBASELINE=1 reuses an existing baseline file)
#   - if `bench.solver_check_cmd` defined → run it
#
# Per-phase:
#   - if `bench.unified_prepare_before_dir` matches the current dir,
#     materialize its EVOLVE-BLOCK via `python -m _lib.prepare_phase <bench>`.
#   - run openevolve-run.py with $INPUT_DIR/_lib/evaluator_entry.py as
#     evaluator (OPENEVOLVE_BENCH_ROOT is exported).
#   - non-last phases: `python -m _lib.extract_best <bench> <phase>`.
#
# Env knobs honored:
#   OPENEVOLVE_PARALLEL_SOLVERS  concurrent solver subprocesses
#   OPENEVOLVE_CORE_RANGE        explicit taskset core range N-M
#                                (also set via --pin)
#   OPENEVOLVE_PROFILE           "small"|"large" — phase modules read it for
#                                W=1 vs W=8 switching (cpsat).
#   SKIP_REBASELINE=1            reuse existing cache/local_baseline.json

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <bench> [<phase>] [--pin N-M] [--extract-only] [--iterations N] [--profile small|large] [extra flags]" >&2
    echo "       omit <phase> to run all phases sequentially" >&2
    echo "       <bench> = dir name under input/ (e.g. cpsat-bench, z3-bench)" >&2
}

if [ $# -lt 1 ]; then
    usage
    exit 2
fi

BENCH="$1"; shift

PHASE=""
if [ $# -ge 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
    PHASE="$1"; shift
fi

INPUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$INPUT_DIR/$BENCH/evolve"
CONFIG_YAML="$ROOT/config.yaml"

if [ ! -d "$ROOT" ]; then
    echo "bench evolve dir not found: $ROOT" >&2
    exit 2
fi
if [ ! -f "$CONFIG_YAML" ]; then
    echo "missing $CONFIG_YAML" >&2
    exit 2
fi

_BENCH_EXPORTS="$(cd "$INPUT_DIR" && python3 -m _lib.load_bench_config "$CONFIG_YAML")"
eval "$_BENCH_EXPORTS"

: "${PHASE_DIRS:?bench.phases missing in $CONFIG_YAML}"

read -r -a _DIRS <<< "$PHASE_DIRS"
read -r -a _ITERS <<< "${PHASE_ITERS:-}"
N_PHASES=${#_DIRS[@]}
LAST_PHASE=$N_PHASES

EXTRACT_ONLY=0
PIN_RANGE=""
PROFILE="small"
PASSTHROUGH=()
while [ $# -gt 0 ]; do
    case "$1" in
        --extract-only) EXTRACT_ONLY=1; shift ;;
        --pin)          PIN_RANGE="$2"; shift 2 ;;
        --pin=*)        PIN_RANGE="${1#--pin=}"; shift ;;
        --profile)      PROFILE="$2"; shift 2 ;;
        --profile=*)    PROFILE="${1#--profile=}"; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              PASSTHROUGH+=("$1"); shift ;;
    esac
done
set -- "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"

case "$PROFILE" in
    small|large) ;;
    *) echo "--profile must be 'small' or 'large' (got: $PROFILE)" >&2; exit 2 ;;
esac
export OPENEVOLVE_PROFILE="$PROFILE"

if [ "$PROFILE" = "small" ]; then
    OUTPUT_DIR="openevolve_output"
else
    OUTPUT_DIR="openevolve_output_${PROFILE}"
fi

if [ -z "$PHASE" ]; then
    if [ "$EXTRACT_ONLY" = "1" ]; then
        echo "--extract-only requires explicit <phase>" >&2
        exit 2
    fi
    PHASES_TO_RUN=()
    for ((i = 1; i <= N_PHASES; i++)); do PHASES_TO_RUN+=("$i"); done
    RUN_ALL=1
else
    if [ "$PHASE" -lt 1 ] || [ "$PHASE" -gt "$N_PHASES" ]; then
        echo "phase must be in {1..$N_PHASES} (got: $PHASE)" >&2
        exit 2
    fi
    PHASES_TO_RUN=("$PHASE")
    RUN_ALL=0
fi

if [ -n "$PIN_RANGE" ]; then
    if ! [[ "$PIN_RANGE" =~ ^[0-9]+(-[0-9]+)?$ ]]; then
        echo "--pin expects N or N-M (got: $PIN_RANGE)" >&2
        exit 2
    fi
    export OPENEVOLVE_CORE_RANGE="$PIN_RANGE"
fi

REPO_ROOT="$(cd "$INPUT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/openevolve-run.py"
EVALUATOR_ENTRY="$INPUT_DIR/_lib/evaluator_entry.py"

export OPENEVOLVE_BENCH_ROOT="$ROOT"
echo "[run_phase] bench=$BENCH profile=$PROFILE output=$OUTPUT_DIR"

# ============ pre-flight ============
if [ "$EXTRACT_ONLY" != "1" ]; then
    if [ ! -f "$RUNNER" ]; then
        echo "openevolve-run.py not found at $RUNNER" >&2
        exit 1
    fi
    if [ ! -f "$EVALUATOR_ENTRY" ]; then
        echo "evaluator_entry not found at $EVALUATOR_ENTRY" >&2
        exit 1
    fi

    if [ ! -f "$ROOT/cache/stage1_sample.json" ]; then
        echo "[run_phase] cache missing — running _lib.sampler..."
        (cd "$INPUT_DIR" && python3 -m _lib.sampler "$BENCH")
    fi

    if [ -f "$ROOT/cache/local_baseline.json" ] && [ "${SKIP_REBASELINE:-0}" = "1" ]; then
        echo "[run_phase] SKIP_REBASELINE=1 — reusing cache/local_baseline.json"
    else
        echo "[run_phase] running _lib.rebaseline (set SKIP_REBASELINE=1 to skip)..."
        (cd "$INPUT_DIR" && python3 -m _lib.rebaseline "$BENCH") || \
            echo "warning: _lib.rebaseline finished with mismatches; evaluator falls back to raw_ms for those."
    fi

    if [ -n "${SOLVER_CHECK_CMD:-}" ]; then
        if ! eval "$SOLVER_CHECK_CMD" >/dev/null 2>&1; then
            echo "warning: solver check failed. ${SOLVER_INSTALL_HINT:-}" >&2
        fi
    fi

    export SKIP_REBASELINE=1
fi

# ============ phase runner ============
run_one_phase() {
    local phase="$1"
    shift
    local dir="${_DIRS[$((phase - 1))]}"
    local iter=""
    if [ "${#_ITERS[@]}" -ge "$phase" ]; then
        iter="${_ITERS[$((phase - 1))]}"
    fi

    if [ "$EXTRACT_ONLY" = "1" ]; then
        if [ "$phase" -eq "$LAST_PHASE" ]; then
            echo "--extract-only not supported for phase $LAST_PHASE" >&2
            return 2
        fi
        (cd "$INPUT_DIR" && python3 -m _lib.extract_best "$BENCH" "$phase" --from-checkpoints)
        return 0
    fi

    local prep_here=0
    if [ -n "${UNIFIED_PREPARE_BEFORE_DIR:-}" ]; then
        [ "$dir" = "$UNIFIED_PREPARE_BEFORE_DIR" ] && prep_here=1
    elif [ "$phase" -eq "$LAST_PHASE" ]; then
        prep_here=1
    fi
    if [ "$prep_here" = "1" ]; then
        local missing=0
        for ((i = 1; i < phase; i++)); do
            if [ ! -f "$ROOT/cache/phase${i}_best.json" ]; then
                echo "phase $phase ($dir) requires cache/phase${i}_best.json" >&2
                missing=1
            fi
        done
        [ "$missing" = "1" ] && return 1
        echo "[run_phase] materializing $dir EVOLVE-BLOCK via _lib.prepare_phase..."
        (cd "$INPUT_DIR" && python3 -m _lib.prepare_phase "$BENCH")
    fi

    cd "$ROOT/$dir"
    echo "[run_phase] === bench=$BENCH phase=$phase dir=$dir ${iter:+iter=$iter }cwd=$(pwd) ==="

    local iter_flag=()
    if [ -n "$iter" ]; then
        iter_flag=(--iterations "$iter")
    fi

    python3 "$RUNNER" \
        initial_program.py \
        "$EVALUATOR_ENTRY" \
        --config "$ROOT/config.yaml" \
        --output "$OUTPUT_DIR" \
        "${iter_flag[@]+"${iter_flag[@]}"}" \
        "$@"

    if [ "$phase" -lt "$LAST_PHASE" ]; then
        echo "[run_phase] extracting best for phase $phase..."
        (cd "$INPUT_DIR" && python3 -m _lib.extract_best "$BENCH" "$phase")
    fi
}

_LAST_RUN=""
for p in "${PHASES_TO_RUN[@]}"; do
    run_one_phase "$p" "$@"
    _LAST_RUN="$p"
done

# Finalize when last phase was reached (run-all OR explicit last phase).
# Skipped for --extract-only.
if [ "$EXTRACT_ONLY" != "1" ] && [ "$_LAST_RUN" = "$LAST_PHASE" ]; then
    echo "[run_phase] finalizing → $ROOT/final_program.py"
    (cd "$INPUT_DIR" && python3 -m _lib.finalize "$BENCH")
fi

if [ "$RUN_ALL" = "1" ]; then
    echo "[run_phase] all $N_PHASES phases completed for $BENCH."
elif [ "${PHASES_TO_RUN[0]}" -lt "$LAST_PHASE" ]; then
    echo "[run_phase] next: $0 $BENCH $((${PHASES_TO_RUN[0]} + 1))"
fi
