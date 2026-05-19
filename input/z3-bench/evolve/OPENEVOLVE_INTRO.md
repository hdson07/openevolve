# OpenEvolve 소개 — Z3 파라미터 튜닝 맥락

실행/구조는 [README.md](README.md) 참고. 이 문서는 OpenEvolve 개념, 본 프로젝트의 목적, 설계 결정, 로드맵을 정리.

---

## 1. OpenEvolve 빠른 소개 (처음 보는 사람용)

### 1.1 한 줄 요약

**OpenEvolve = LLM × 진화 알고리즘.** 사람이 정의한 "코드 조각"과 "평가 함수"를 받아, LLM이 코드를 반복적으로 변이시키고 점수가 높은 변이만 살려나간다. Google DeepMind의 AlphaEvolve 시스템을 오픈소스로 재구현한 프레임워크.

### 1.2 동작 원리 (1 iteration)

```
┌───────────────────────────────────────────────────────────────┐
│ 1. Database에서 부모 프로그램 K개 샘플링 (MAP-Elites + islands) │
│ 2. LLM 프롬프트 구성:                                          │
│      - system_message (config.yaml에서)                       │
│      - 부모 코드 + 부모들의 점수/메트릭                          │
│      - 과거 변이 일부 (inspiration)                            │
│      - 이전 변이의 artifacts (디버깅 신호)                       │
│ 3. LLM이 새 코드 생성 (diff 또는 full rewrite)                 │
│ 4. 새 프로그램을 evaluator.py에 넘김                            │
│ 5. evaluator가 metrics dict 반환 (예: combined_score: 0.73)   │
│ 6. Database에 (code, metrics, artifacts) 저장                 │
│ 7. checkpoint_interval마다 디스크 저장                          │
└───────────────────────────────────────────────────────────────┘
```

위를 `--iterations N`번 반복. 종료 시 `openevolve_output/best/best_program.py`에 최고 점수 변이 저장.

### 1.3 핵심 개념

#### EVOLVE-BLOCK
초기 프로그램(`initial_program.py`) 안에서 **LLM이 수정해도 되는 영역**을 마커로 표시:

```python
# 고정된 코드 (수정 금지)
import some_lib

# EVOLVE-BLOCK-START
# 이 안의 코드만 LLM이 변이시킴
def my_algorithm():
    return 42
# EVOLVE-BLOCK-END

# 고정된 코드 (인터페이스 보존용)
def run():
    return my_algorithm()
```

블록 밖은 인터페이스/타입/평가 함수 호출 규약 등을 유지하는 부분. 이 프로젝트에서는 EVOLVE-BLOCK 안에 Z3 파라미터 **dict 리터럴**을 두어 LLM이 키/값을 변이시킴.

#### Evaluator
`evaluator.py`는 **단 하나의 함수**(`evaluate(program_path)`)를 노출. 변이된 프로그램을 받아 점수 dict를 돌려준다:

```python
from openevolve.evaluation_result import EvaluationResult

def evaluate(program_path):
    # 1. 프로그램 import
    # 2. 실행 / 측정
    # 3. metrics 계산
    return EvaluationResult(
        metrics={"combined_score": 0.73, "sub_metric_a": 1.5, ...},
        artifacts={"summary": "...", "per_problem": [...]},   # 다음 LLM 호출의 컨텍스트로 사용됨
    )
```

- **`metrics`**: 진화 압력. `combined_score` 키가 주 목적함수. 다른 키들은 부수 모니터링.
- **`artifacts`**: 점수에 영향 안 줌. 단, LLM에 다음 라운드 컨텍스트로 들어가서 "왜 실패했는지/뭐가 좋아졌는지" 학습 신호가 됨. 예: 에러 메시지, 잘못된 키 이름, 인스턴스별 speedup 분포.

#### Cascade evaluation
한 변이를 평가하는 데 비용이 크면 단계별로 컷:

```
evaluate_stage1(program_path) → 빠른 검증 (이 프로젝트: 5문제)
  └─ score 낮으면 즉시 컷 (cascade_thresholds 비교)
  └─ score 통과 시 ↓
evaluate_stage2(program_path) → 본 평가 (이 프로젝트: 전체 50문제)
```

명백히 망가진 변이(시드 위반, invalid key, 큰 회귀)는 stage1에서 거름. LLM 시간/달러 절약.

#### MAP-Elites + Islands (다양성 유지)
- **MAP-Elites**: 코드를 다차원 feature grid에 매핑 → 각 셀의 챔피언만 유지. 단순히 "최고 점수 1개"가 아니라 "각 영역의 최고"를 보존 → 국소 최적 탈출.
- **Islands**: 독립된 N개의 population이 따로 진화. 주기적으로 migration. 한 island의 조기 수렴이 전체에 퍼지지 않게 함.

이 프로젝트 설정: `num_islands: 3`, `population_size: 50`, `archive_size: 20`.

#### Diff-based evolution
`diff_based_evolution: true`면 LLM이 전체 파일이 아니라 **search/replace 블록만** 출력. 큰 파일에서 토큰 절약 + 의도 명확.

### 1.4 OpenEvolve가 받는 입력 (총 3개 파일)

| 파일 | 역할 |
|---|---|
| `initial_program.py` | 시작점. EVOLVE-BLOCK 안 코드만 진화. 인터페이스는 evaluator와 합의 |
| `evaluator.py` | `evaluate(program_path) -> EvaluationResult` 함수 1개 노출 |
| `config.yaml` | LLM 모델, 반복 횟수, population, prompt system_message 등 |

호출:
```bash
python openevolve-run.py \
    initial_program.py \
    evaluator.py \
    --config config.yaml \
    --iterations 100
```

### 1.5 출력

```
<cwd>/openevolve_output/
├── best/
│   └── best_program.py              # 최고 score 변이
├── checkpoints/
│   ├── checkpoint_10/               # checkpoint_interval마다
│   ├── checkpoint_20/
│   └── ...
└── logs/
    └── openevolve_*.log
```

`--checkpoint <path>` 옵션으로 중단 지점에서 재개 가능.

### 1.6 이 프로젝트에서 OpenEvolve의 적용 방식

| OpenEvolve 개념 | 이 프로젝트에서 어떻게 쓰이나 |
|---|---|
| EVOLVE-BLOCK | Z3 파라미터 dict 리터럴 (`OPT_SLS_OVERRIDES = {...}` 등) |
| 진화 단위 | 알고리즘 코드 아니라 **dict 키/값** (이름 추가/제거/값 변경) |
| Evaluator | 변이된 dict를 `subprocess`로 `z3 ...`에 넘겨 50개 SMT2 풀고 점수화 |
| metrics | `combined_score = geomean(speedup) × solved_rate²` |
| artifacts | 인스턴스별 (sha, baseline_ms, elapsed_ms, speedup, timeout) — LLM이 다음 라운드에 "어느 문제가 느려졌는지" 확인 가능 |
| Cascade | stage1=5문제 15s, stage2=50문제 120s |
| Phase 분할 | 단일 OpenEvolve 실행이 아니라 **4회 순차 실행**. 각 phase는 다른 `initial_program.py` 사용, 이전 phase의 winner를 import |

### 1.7 더 읽기

- 메인 README: `<repo_root>/README.md`
- 다른 예제: `examples/function_minimization/` (가장 간단), `examples/llm_prompt_optimization/`, `examples/circle_packing/`
- 기본 config 전체: `configs/default_config.yaml`
- 아키텍처: `CLAUDE.md` (개발자용 노트)

---

## 2. 목적과 접근

- **타깃**: `z3_applied_params` 19개(베이스라인)에서 출발 → Z3 4.13.x 전체 파라미터 공간(opt./sat./smt./sls./parallel./global, ~250키) 탐색
- **방법**: OpenEvolve로 `initial_program.py` 안의 dict 리터럴을 LLM이 변이. EVOLVE-BLOCK 마커 사이의 파라미터 dict만 진화 대상
- **베이스라인**: `problems.jsonl`의 `applied_params_hash = 543b29...` 행들. 인스턴스별 `elapsed_ms` + `result`를 기준값으로 사용
- **솔버 실행**: subprocess로 `z3 -T:<sec> -smt2 key=value ... file.smt2` 호출 → 격리 + 타임아웃 강제

## 3. Phase 분할 (옵션 b)

| Phase | EVOLVE 대상 | 고정(locked) | 키 수 | iterations | 목적 |
|---|---|---|---|---|---|
| **P1** | `opt.*` + `sls.*` | sat/smt/parallel 베이스라인 + 시드 3종 | ~34 | 80 | MaxSAT 엔진 선택, SLS local search 튜닝 |
| **P2** | `sat.*` | P1 best `opt.*+sls.*` + smt/parallel 베이스라인 | ~121 | 150 | CDCL 코어 (preprocessing/restart/branching) |
| **P3** | `smt.*` (`auto_config=false` 강제) | P1+P2 best | ~97 | 120 | 산술/양화자 — LIA-heavy 워크로드에 영향 큼 |
| **P4** | P1∪P2∪P3 best 통합 | 없음 (locked 키만 유지) | union | 60 | 상호작용 보정. 짧은 local refinement |

**고정 키 (locked)** — 전체 phase 변경 금지, evaluator가 위반 시 0점:
- `sat.random_seed = 0`
- `smt.random_seed = 0`
- `sls.random_seed = 0`
- `parallel.enable = False`

**Phase 간 핸드오프**: 자동. P{N} 종료 후 `run_phase.sh`가 `extract_best.py N` 자동 호출 → `shared/phase{N}_best.json` 작성 → P{N+1}이 import.

P4 시작 시 `prepare_phase4.py` 자동 실행 → `phase4_unified/initial_program.py`의 EVOLVE-BLOCK을 union dict literal로 머터리얼라이즈 (LLM이 diff 편집 가능하도록).

## 4. 스코어링

```
per_problem:
    match baseline result → speedup = baseline_ms / elapsed_ms
    mismatch (regression/unknown/timeout) → 1e-6 (geomean에 강한 페널티)

aggregate:
    combined_score = geomean(speedup) * solved_rate^2
```

- `solved_rate^2`: 정답률이 핵심 게이트. 1회 회귀도 강하게 패널티
- `geomean(speedup)`: 큰 인스턴스가 합산 지배하지 않도록
- baseline 그대로면 `combined_score ≈ 1.0`
- 부수 메트릭: `regressions`, `solved/total`, `geomean_speedup`

## 5. 주요 설계 결정

| 항목 | 선택 | 이유 |
|---|---|---|
| Phase 핸드오프 | 자동 (`run_phase.sh` → `extract_best.py`) | 사람 개입 줄임 |
| 메트릭 | `geomean(speedup) × solved_rate²` | 큰 인스턴스 지배 방지 + 정답률 강하게 게이트 |
| Z3 실행 | subprocess CLI | 프로세스 격리, 타임아웃 강제, 한 문제 크래시 영향 차단 |
| Stage1 샘플 | stratified 5문제, seed=42 | Sat/Unsat × 빠름/느림 골고루, 재현 가능 |
| Locked 키 | 시드 3종 + parallel.enable | 비교 공정성, 단일스레드 일관성 |
| `smt.auto_config` | P3에서 False 강제 | True면 다른 smt.* 옵션이 silently override됨 |
| `parallel_evaluations` | 1 | z3 메모리 4GB+ 인스턴스 존재, OOM 위험 |
| Phase 4 EVOLVE-BLOCK | 머터리얼라이즈된 literal dict | LLM이 diff 편집 가능해야 진화 가능 |

## 6. 검증 상태

- `python build_stage1_sample.py` → 5문제 stratified 샘플 생성 (완료)
- 4개 phase `initial_program.py` import 확인:
  - phase1: 46 키 (BASELINE 19 + OVERRIDES 34, 일부 키 중복)
  - phase2: 135 키
  - phase3: 114 키
  - phase4: 19 키 (BASELINE만 — phases 1-3 이후 prepare_phase4.py가 채움)
- `score.py` 시뮬레이션: 2 speedup + 1 timeout + 1 regression + 1 slowdown → combined ≈ 0.002 (correctness gate 강하게 작동 확인)

## 7. 비용/시간 추정

- baseline 평균 elapsed_ms ≈ 24,229 ms → 변이당 stage2 full run ≈ 50 × 24s = 1200s = 20분 (평균)
- P1 80 iter × 평균 20분 ≈ 27시간 (worst-case 비현실적, cascade로 대부분 stage1에서 컷)
- P2 150 iter × 20분 ≈ 50시간
- 비용 절감: `OPENEVOLVE_MAX_PROBLEMS=20`으로 stage2도 축소 가능. 또는 stage1 cascade threshold 0.5+로 상향 → 약한 변이 조기 컷 비율↑

## 8. 향후 작업 후보

1. 컨테이너에서 `z3 -pmd` 캡쳐 → invalid key 사전 필터
2. baseline 변이 stage1 1회 평가 sanity check
3. `docker-run.sh`에 `-e OPENAI_API_KEY` 자동 전달 추가
4. LLM 모델 선택 (Gemini 무료 티어 vs 사내 모델 vs OpenAI)
5. 변이 결과 시각화 (`scripts/visualizer.py --path .../checkpoint_K/`)
6. final 검증: P4 best를 problems.jsonl 전수 50문제에 대해 재실행, speedup 분포 리포트
