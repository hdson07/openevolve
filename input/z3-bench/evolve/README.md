# z3-bench — Z3 SMT 솔버 파라미터 튜닝

Z3 SMT 솔버 (v4.13.x) 파라미터를 진화적 탐색으로 튜닝. 데이터셋:
`input/z3-bench/raw-data/` (50개 SMT2 인스턴스, ~13k Int / 19k Bool / 40 Real
변수, ~2k soft + 105k hard 제약). 목표: baseline 대비 wall-clock 시간 단축,
정답성 (Sat/Unsat) 보존.

플랫폼 전반 구조는 [`input/README.md`](../../README.md), OpenEvolve 개념은
[`OPENEVOLVE_INTRO.md`](OPENEVOLVE_INTRO.md) 참고. 본 문서는 z3-bench 고유
사항만 다룬다.

## 디렉토리 구조

```
input/z3-bench/
├── raw-data/                          # <sha>.smt2 + meta.jsonl
├── problems.jsonl                     # baseline 실행 기록 (50 rows)
└── evolve/
    ├── config.yaml                    # bench / LLM / clustering / evaluation
    ├── params.json                    # Z3 파라미터 카탈로그
    ├── adapter.py                     # solver hooks
    ├── _solve_worker.py               # z3 Python binding subprocess
    ├── phase1_opt_sls/                # opt.* + sls.*
    ├── phase2_sat/                    # sat.* (CDCL core)
    ├── phase3_smt/                    # smt.* (theories, quantifier, arith)
    ├── phase4_unified/                # 통합 refinement (자동 머터리얼)
    └── cache/                         # 생성물, 삭제 안전
        ├── stage{1..4}_sample.json
        ├── local_baseline.json
        └── phase{N}_best.json
```

## 평가 흐름

`config.yaml` `bench.evaluation`:

| 키 | 값 |
|---|---|
| `repeats` | 10 (10회 평균) |
| `score_mode` | `speedup` (wall-clock) |
| `enable_size_buckets` | `false` (z3는 단일 surface) |
| `enable_outlier_stage` | `false` |

Cascade: stage1 (5문제) → stage2 (5문제) → stage3 (5문제, outlier) → stage4
(20문제, 전체 spread). 각 stage gate는 `cascade_thresholds`.

정답성 regression (baseline decisive + variant mismatch)은 abort + `1e-6`
penalty. invalid_param은 즉시 0점 + 어떤 키인지 artifact.

## Clustering (config.yaml `bench.clustering`)

| 키 | 값 |
|---|---|
| `method` | `kmeans` |
| `feature` | `features.num_hard_constraints` (dominant size signal) |
| `n_clusters` | 5 |
| `max_baseline_ms` | 300000 (5분 cap) |
| `stage_sizes` | stage1=5, stage2=5, stage3=5, stage4=20 |
| `stage_clusters` | stage1=c0+c1, stage2=c2+c3, stage3=c4, stage4=전체 |

`python -m _lib.sampler z3-bench`로 `cache/stage{1..4}_sample.json` 생성.

## Phase별 surface

cpsat과 달리 z3는 SIZE_BUCKETS / STAGE3_OVERRIDES 미사용. 단일 surface
`OVERRIDES` 만 LLM 변이.

| Phase | EVOLVE-BLOCK | inheritance |
|---|---|---|
| 1 (opt_sls) | `OVERRIDES = {}` | BASELINE only |
| 2 (sat) | `OVERRIDES = {}` | + cache/phase1_best.json |
| 3 (smt) | `OVERRIDES = {}` | + cache/phase2_best.json |
| 4 (unified) | `UNIFIED_OVERRIDES = {}` | `_lib.prepare_phase`가 phase{1,2,3}_best union으로 자동 채움 |

`get_params()` 적용 순서: `BASELINE → prior_phase_best → current OVERRIDES`.

## Quick start

```bash
# 1. 전체 pipeline (sampler + rebaseline + 4 phases 순차)
./input/run_phase.sh z3-bench --pin 2-7

# 2. 단계별
python -m _lib.sampler z3-bench              # cache/stage{1..4}_sample.json
python -m _lib.self_test z3-bench            # baseline sanity (stage1)
python -m _lib.rebaseline z3-bench           # cache/local_baseline.json (10회 평균)
./input/run_phase.sh z3-bench 1 --pin 2-7
./input/run_phase.sh z3-bench 2 --pin 2-7
./input/run_phase.sh z3-bench 3 --pin 2-7
./input/run_phase.sh z3-bench 4 --pin 2-7

# 3. 최종 검증 — run_phase.sh가 마지막 phase 후 자동 생성한 final_program.py 사용
python -m _lib.final_verify z3-bench \
    input/z3-bench/evolve/final_program.py
```

마지막 phase 완료 후 `_lib.finalize`가 자동 실행 →
`<bench>/evolve/final_program.py`에 phase4 best_program.py 복사. 이 파일이
canonical evolved 결과. 수동 재생성: `python -m _lib.finalize z3-bench`.

각 non-final phase 완료 후 `_lib.extract_best`가
`cache/phaseN_best.json` 자동 생성. Phase 4 시작 전
`_lib.prepare_phase`가 EVOLVE-BLOCK 머터리얼.

## Score 공식 (speedup mode)

```
combined_score = weighted_geomean(speedup) * solved_rate^2 * efficiency^STATS_WEIGHT

speedup       = baseline_ms / variant_ms           (match 시)
              = 1e-6                                (regression 시)
weight        = baseline_ms                        (긴 문제 dominate)
efficiency    = geomean over {conflicts(w=2), decisions(w=1.5), propagations(w=0.5)}
                of (baseline_stat + 1) / (variant_stat + 1), clipped [0.1, 10]
```

- 매치 (baseline Sat→variant Sat 또는 Unsat→Unsat) 시 wall-clock ratio가
  점수에 기여.
- Mismatch (baseline decided + variant Unknown/timeout/opposite) 시 `1e-6`
  → 한 문제 regression이 geomean을 크게 감점.
- baseline이 Unknown인 경우 variant가 풀어내면 개선으로 카운트, regression
  아님.

## Locked params

`sat.random_seed=0`, `smt.random_seed=0`, `sls.random_seed=0`,
`parallel.enable=false`, `threads=1`. 위반 시 `combined_score=0`.

## 디버깅 / 트러블슈팅

| 증상 | 대응 |
|---|---|
| `invalid_param: <key>` artifact | params.json 카탈로그에 누락된 키. `_lib.params_catalog`가 catch하거나 z3 binary가 reject. params.json `groups[*].params[*]`에 추가하거나 LLM prompt에 명시. |
| Result regression abort | baseline은 Sat/Unsat인데 variant Unknown/timeout. presolve / SLS / restart 튜닝이 completeness 깬 경우 많음. |
| 로컬 baseline mismatch | `_lib.rebaseline`이 raw baseline과 불일치 결과 → evaluator는 raw_ms fallback. Z3 binary 버전 차이 또는 hardware noise. |

## 참고

- 파라미터 카탈로그 + 검증: `params.json` (rich schema). 1265개 Z3 4.13.x
  키 중 LLM이 실제로 변이하는 ~27개 그룹화 + type/enum/range/desc 명시.
- 환경 변수: [`input/README.md`](../../README.md#environment-knobs) 참고.
- Z3 도커 셋업 / Claude Code 백엔드 / CPU 핀닝: 이전 버전 본 문서
  (`git log -p`) 또는 `docker-run.sh --help` 참고.
