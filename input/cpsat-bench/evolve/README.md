# cpsat-bench — OR-tools CP-SAT 파라미터 튜닝

OR-tools CP-SAT (`ortools.sat.python.cp_model`) 파라미터를 진화적 탐색으로
튜닝. 데이터셋: `input/cpsat-bench/raw-data/` (85개 OPTIMAL 인스턴스, 바이너리
`CpModelProto`). 목표: baseline 대비 `deterministic_time` 최소화, 정답성
(`OPTIMAL` / `FEASIBLE`) 유지.

플랫폼 전반 구조는 [`input/README.md`](../../README.md) 참고. 본 문서는
cpsat-bench 고유 사항만 다룬다.

## 디렉토리 구조

```
input/cpsat-bench/
├── raw-data/                          # <sha>.cpsat.pb + meta.jsonl
├── problems.jsonl                     # baseline 실행 기록 (855 rows)
└── evolve/
    ├── config.yaml                    # bench / LLM / clustering / evaluation
    ├── params.json                    # CP-SAT 파라미터 카탈로그
    ├── adapter.py                     # solver hooks
    ├── _solve_worker.py               # ortools 호출 subprocess
    ├── phase1_search/                 # search / subsolvers
    ├── phase2_presolve/               # presolve / probing / symmetry
    ├── phase3_lp_cuts/                # LP / cuts / MIP-bridge (W=8)
    ├── phase4_unified/                # unified refinement (W=8, 자동 머터리얼)
    ├── phase5_custom_subsolvers/      # custom subsolver portfolio (W=8)
    └── cache/                         # 생성물, 삭제 안전
        ├── stage{1..4}_sample.json
        ├── local_baseline.json
        ├── phase{N}_best.json
        ├── phase{N}_buckets.json      # SIZE_BUCKETS 결과 (opt-in)
        └── phase{N}_stage3.json       # STAGE3_OVERRIDES 결과 (opt-in)
```

## 평가 흐름

`config.yaml` `bench.evaluation`:

| 키 | 값 |
|---|---|
| `repeats` | 10 (10회 평균) |
| `score_mode` | `cost` (deterministic_time + cost_ratio) |
| `time_metric` | `dtime` |
| `enable_size_buckets` | `true` — 문제 크기별 override |
| `enable_outlier_stage` | `true` — stage3 outlier 전용 override |

Cascade: stage1 (10문제, 작은-중간 클러스터) → stage2 (10문제) → stage3
(5문제, outlier) → stage4 (20문제, 전체 spread). 각 stage gate는
`cascade_thresholds`.

비결정 변형 (UNKNOWN / INFEASIBLE)은 abort 안 함 — 실측 (느린) timeout
ratio가 점수에 반영 + `solved_rate^2` drop이 추가 penalty.

## Clustering (config.yaml `bench.clustering`)

| 키 | 값 |
|---|---|
| `method` | `kmeans` |
| `feature` | `features.num_constraints` |
| `n_clusters` | 5 |
| `max_baseline_ms` | 120000 (> 120s outlier 제외) |
| `stage_sizes` | stage1=10, stage2=10, stage3=5, stage4=20 |
| `stage_clusters` | stage1=c0+c1, stage2=c2+c3, stage3=c4, stage4=전체 |

`python -m _lib.sampler cpsat-bench`로 `cache/stage{1..4}_sample.json` 생성.

## Phase별 surface

각 phase `initial_program.py`의 EVOLVE-BLOCK에는 세 surface:

| Surface | 적용 조건 | 용도 |
|---|---|---|
| `GLOBAL_OVERRIDES` | 모든 문제 | phase 기본 튜닝 |
| `SIZE_BUCKETS` | `problem["size"]` (= num_constraints)이 bucket upper 미만 | 크기별 cuts / probing / subsolver mix tradeoff |
| `STAGE3_OVERRIDES` | `stage == "stage3"` AND `problem["is_outlier"]` | long-tail outlier 전용 |

`get_params(problem, stage)` 적용 순서:
`BASELINE → GLOBAL_OVERRIDES → SIZE_BUCKETS 매치 → STAGE3_OVERRIDES (gated)
→ PHASE_LOCKED`.

`size` 값은 `adapter.get_problem_size(features)` (= `num_constraints`)에서
나옴.

## Phase 5: custom subsolvers

Phase 1-4가 top-level 파라미터를 튜닝하는 반면, Phase 5는 portfolio에
**custom subsolver를 추가**한다 (inherited phase4 top-level config는 건드리지
않음).

이유: top-level 파라미터는 모든 subsolver (LNS worker 포함)에 적용됨.
expensive propagation을 top-level에 켜면 LNS도 같이 비싸져 portfolio가
무너짐. Isolated subsolver는 downside를 한 worker로 bound하면서, 도움이
되면 solution / variable bound sharing으로 portfolio 전체에 lift.

Phase 5 EVOLVE-BLOCK:

| Surface | 적용 |
|---|---|
| `CUSTOM_SUBSOLVERS` | 모든 문제 |
| `STAGE3_CUSTOM_SUBSOLVERS` | stage3 outlier 전용 |

각 entry:
```python
{
    "name": "max_lp_heavy",
    "params": {"linearization_level": 2, "add_mir_cuts": True,
               "max_num_cuts": 12000, "cut_level": 2},
    "min_constraints": 50000,
    "max_constraints": None,
}
```

`_solve_worker.py`가 `subsolver_params` 전체를 standalone proto로 빌드 후
대입 — recent ortools (9.15+)의 nested repeated message in-place mutation
미지원 우회.

Locked: `random_seed=0`, `num_search_workers=8`, `interleave_search=True`
(cross-worker sharing 전제).

Phase 5는 terminal — extract 단계 없음. Phase 4가 `unified_prepare_before_dir`
target이라 `python -m _lib.prepare_phase cpsat-bench`로 미리 머터리얼됨.

## Quick start

```bash
# 1. raw-data 채우기 (최초 1회)
cd input/cpsat-bench/raw-data && bash load_script.sh && cd -

# 2. 전체 pipeline (sampler + rebaseline + 5 phases 순차)
./input/run_phase.sh cpsat-bench --pin 2-7

# 3. 단계별
python -m _lib.sampler cpsat-bench           # cache/stage{1..4}_sample.json
python -m _lib.self_test cpsat-bench         # baseline sanity (stage1)
python -m _lib.rebaseline cpsat-bench        # cache/local_baseline.json (10회 평균)
./input/run_phase.sh cpsat-bench 1 --pin 2-7
./input/run_phase.sh cpsat-bench 2 --pin 2-7
./input/run_phase.sh cpsat-bench 3 --pin 2-7
./input/run_phase.sh cpsat-bench 4 --pin 2-7
./input/run_phase.sh cpsat-bench 5 --pin 2-7

# 4. 최종 검증 — run_phase.sh가 마지막 phase 후 자동 생성한 final_program.py 사용
python -m _lib.final_verify cpsat-bench \
    input/cpsat-bench/evolve/final_program.py
```

마지막 phase 완료 후 `_lib.finalize`가 자동 실행 →
`<bench>/evolve/final_program.py`에 phase5 best_program.py 복사. 이 파일이
canonical evolved 결과. 수동 재생성: `python -m _lib.finalize cpsat-bench`.

각 non-final phase 완료 후 `_lib.extract_best`가
`cache/phaseN_best.json` (+ buckets / stage3) 자동 생성.
다음 phase는 그 파일을 inheritance source로 읽음.

## Worker count 정책

| Phase | `num_search_workers` | 이유 |
|---|---|---|
| 1 | 1 (small) / 8 (large profile) | 다른 knob noise 차단 |
| 2 | 1 (small) / 8 (large profile) | presolve clean signal |
| 3 | 8 | subsolver mix engaged |
| 4 | 8 | 통합 refinement |
| 5 | 8 | portfolio sharing 필요 |

`OPENEVOLVE_PROFILE=large`로 phase 1/2도 W=8 운영 가능 (outlier 튜닝 트랙).

## Score 공식 (cost mode)

```
combined_score = geomean( (b_obj/v_obj)^COST_W * time_ratio )
                 * solved_rate^2
                 * efficiency^STATS_WEIGHT

time_ratio  = baseline_dtime / variant_dtime    (primary)
            = baseline_ms    / variant_ms       (dtime 누락 시 fallback)
cost_ratio  = (baseline_obj + eps) / (variant_obj + eps)
```

- `deterministic_time` 은 CP-SAT의 하드웨어 독립적 work measure. wall-clock
  noise (CPU load / NUMA / thermal) 제거됨. `geomean_speedup` = dtime 기반,
  `geomean_wall_speedup` = wall 기반 (진단용).
- 85 baseline 모두 OPTIMAL → variant도 OPTIMAL이면 cost_ratio = 1.0 → score
  = geomean(time speedup).
- Variant가 FEASIBLE-but-worse-objective → cost_ratio < 1로 감점.
- Variant가 UNKNOWN/INFEASIBLE → solved_rate^2 drop + 실측 timeout ratio
  contribution.
- Efficiency factor: `num_conflicts` (weight 2.0), `num_branches` (weight 1.5)
  ratio.

## Locked params

`random_seed=0` 전역. Phase별 `num_search_workers` lock. Phase 5는 추가로
`interleave_search=True` lock.

위반 시 `combined_score=0` + `locked_violated` artifact. 평가자가
per-problem `get_params(problem, stage)` 호출 후 lock을 defensive하게 재적용
— SIZE_BUCKETS / STAGE3_OVERRIDES로 우회 불가.

## 참고

- 파라미터 카탈로그 + LLM prompt reference: `params.json` (rich schema —
  type, range, default, desc). `_lib.params_catalog` 로 load + validate.
  `config.yaml`의 `prompt.system_message`에 `{{params_reference}}` 토큰으로
  삽입 가능 (prompt 자동 합성).
- 환경 변수: [`input/README.md`](../../README.md#environment-knobs) 참고.
