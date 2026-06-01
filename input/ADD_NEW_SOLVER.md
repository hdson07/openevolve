# Adding a New Solver — Quick Start Guide

새 solver (e.g. `cvc5`, `minizinc`, `picosat`, `vampire`)를 OpenEvolve
파라미터 튜닝 파이프라인에 통합하는 절차. 모든 orchestration은 `_lib/`이
담당하므로 **per-bench 작업물은 4 파일 + N개 phase 모듈**.

---

## 0. 전체 그림

```
input/<solver>-bench/
├── raw-data/                          # 사용자가 제공 — solver run 결과
│   ├── <sha>.<ext>                    # 문제 파일 (binary or text)
│   └── <sha>__<hash>__seed0.meta.jsonl  # optional run metadata
├── problems.jsonl                     # baseline 실행 기록 (필수)
└── evolve/
    ├── config.yaml                    # ① bench / LLM / clustering / evaluation
    ├── params.json                    # ② 솔버 파라미터 카탈로그
    ├── adapter.py                     # ③ 솔버 hooks
    ├── _solve_worker.py               # ④ subprocess entry
    ├── phase1_<name>/initial_program.py
    ├── phase2_<name>/initial_program.py
    └── ... (phase 수 자유)
```

`_lib/`는 한 줄도 수정 안 함. 모든 솔버 의존성은 위 6종에 격리.

---

## 1. 입력 데이터 준비 (사용자 제공)

### 1.1 `input/<solver>-bench/raw-data/`

각 문제 1개당 1개 파일. 확장자 자유 (예: `.smt2`, `.cpsat.pb`, `.mzn`, `.cnf`).

권장 파일명:
```
<problem_sha256>.<ext>
```

(SHA-256은 문제 내용 hash. 재현성 / dedup 기준.)

### 1.2 `input/<solver>-bench/problems.jsonl`

**사용자가 직접 작성하는 필수 input.** `_lib/sampler.py`는 이 파일을 읽기만
함 (생성 안 함). 솔버별 raw-data → problems.jsonl 변환은 솔버별 스크립트로
사용자가 작성 (`_lib`에서 generic화하기엔 meta format이 솔버마다 너무
다름 — cpsat은 `cpsat_response_stats` + protobuf 카운터, z3는
`z3_statistics` + 35종 카운터, 신규 솔버는 자유).

옛 cpsat `build_samples.py` (refactor에서 제거됨)에는 `raw-data/*.meta.jsonl`을
스캔해서 `problems.jsonl`을 생성하는 로직이 있었음. 그 부분은
**cpsat-bench 전용 사용자 스크립트로 따로 보존하거나** `raw-data/load_script.sh`
등에 통합 권장 — `_lib`로 옮기지 않음.

한 줄 = 한 문제. 한 번의 baseline 실행 기록. 필수 필드:

```json
{
  "problem_sha256": "<sha>",
  "<solver>_filename": "<sha>.<ext>",         // adapter.PROBLEM_FILE_FIELD가 가리킴
  "<solver>_status": {                         // adapter.STATUS_FIELD가 가리킴
    "result": "Sat",                           // adapter.DECISIVE_RESULTS 중 하나
    "elapsed_ms": 1234,
    "objective_value": 42.0                    // OBJECTIVE_FIELD 정의 시
  },
  "<solver>_response_stats": {                 // optional (adapter.STATS_FIELD)
    "conflicts": 100,
    "decisions": 200
  },
  "features": {                                // 필수 (clustering 용)
    "num_variables": 1000,
    "num_constraints": 5000,
    "num_hard_constraints": 4000,
    "num_soft_constraints": 1000
  }
}
```

필드명은 자유 (adapter.py에 정확한 키를 알려주면 됨). features 안에 무엇이
들어가는지도 자유 — adapter.get_problem_size(features)가 어떤 키 읽는지 결정.

### 1.3 `meta.jsonl` (optional)

각 (sha, hash, seed) 조합당 1개 파일. cpsat / z3는 이걸 historical 기록으로
유지하지만 `_lib`은 사용 안 함 — `problems.jsonl`만 읽음. 새 솔버는 needed
없으면 생략 가능.

용도: raw-data 갱신 시 `problems.jsonl`을 재구축하는 **사용자 측 스크립트**의
input. `_lib`는 관여 안 함.

### 1.4 `problems.jsonl` 생성 워크플로 (사용자 측)

`_lib` 외부에서 다음 중 하나를 선택:

**옵션 a. 수동 작성** — 새 솔버 / 소규모 dataset.

**옵션 b. 솔버 전용 build 스크립트** — raw-data 스캔 + 변환. 예시:
```bash
# input/<solver>-bench/build_problems_jsonl.py (사용자 작성)
python3 input/<solver>-bench/build_problems_jsonl.py
# → input/<solver>-bench/problems.jsonl 작성
```

옛 cpsat `build_samples.py`의 `_scan_raw()` 함수를 참고용 (git history) →
신규 솔버는 비슷한 패턴으로 자체 스크립트 작성.

**옵션 c. raw-data와 함께 사전 배포** — z3-bench가 사용하는 패턴. dataset
저자가 미리 `problems.jsonl`을 생성해 함께 commit.

---

## 2. 작성할 4개 파일

### 2.1 `evolve/config.yaml` (① bench 설정)

가장 짧은 예시 (필수 부분만):

```yaml
bench:
  phases:
    - dir: phase1_main
      iters: 40
    - dir: phase2_unified
      iters: 40

  unified_prepare_before_dir: phase2_unified
  unified_dict_name: UNIFIED_OVERRIDES

  solver_check_cmd: "command -v <solver_binary>"
  solver_install_hint: "install: brew install <solver>"

  adapter: adapter.py
  params_catalog: params.json
  worker_path: _solve_worker.py

  clustering:
    method: kmeans                     # kmeans | quintile | thresholds
    feature: features.num_constraints  # dotted path into problems.jsonl record
    n_clusters: 5
    max_baseline_ms: 300000            # drop outliers > 5min
    spread: quintile
    stage_sizes: {stage1: 10, stage2: 10, stage3: 5, stage4: 20}
    stage_clusters:
      stage1: [0, 1]
      stage2: [2, 3]
      stage3: [4]
      stage4: [0, 1, 2, 3, 4]

  evaluation:
    repeats: 10                        # 10-run averaging (standard)
    timeout_factor: 1.3
    min_timeout_s: 5
    score_mode: speedup                # speedup | cost
    enable_size_buckets: false         # opt-in: SIZE_BUCKETS surface
    enable_outlier_stage: false        # opt-in: STAGE3_OVERRIDES

parallel_solvers: 2

max_iterations: 40
checkpoint_interval: 10
log_level: "INFO"
random_seed: 42

llm:
  models:
    - name: "claude-sonnet-4-6"
      provider: "claude_code"
      weight: 1.0

prompt:
  system_message: |
    Tune <solver> parameters for <workload>.
    EVOLVE-BLOCK exposes a dict; mutate it to MAXIMIZE combined_score.
    Hard rules:
      - Do NOT modify locked keys (see params.json `locked`).
      - Use only valid <solver> param keys (catalog validation enforced).
      - Score = geomean(speedup) * solved_rate^2 * efficiency^STATS_WEIGHT.

database:
  num_islands: 3

evaluator:
  timeout: 1800
  cascade_evaluation: true
  cascade_thresholds: [1.03, 1.03, 1.03]
  parallel_evaluations: 1
```

### 2.2 `evolve/params.json` (② 파라미터 카탈로그)

```json
{
  "solver": "<solver_name>",
  "version": "<solver_version>",

  "defaults": {
    "<key1>": <value>,
    "<key2>": <value>
  },

  "locked": {
    "<seed_key>": 0
  },

  "groups": {
    "search": {
      "description": "Branching and restart strategy.",
      "params": {
        "<key>": {
          "type": "enum",
          "values": ["a", "b", "c"],
          "default": "a",
          "desc": "What this knob does."
        },
        "<int_key>": {
          "type": "int",
          "default": 100,
          "range": [0, 10000],
          "desc": "..."
        },
        "<bool_key>": {
          "type": "bool",
          "default": true,
          "desc": "..."
        },
        "<list_key>": {
          "type": "list",
          "element_type": "subsolver",
          "default": [],
          "desc": "..."
        }
      }
    }
  },

  "subsolver_names": []
}
```

지원 타입: `int`, `float`, `bool`, `str`, `enum` (with `values`), `list` (with
optional `element_type: subsolver`).

`defaults`는 BASELINE 역할 (구 `baseline_params.py` 대체). `locked`는 LLM이
바꾸면 즉시 `combined_score=0`이 되는 키.

### 2.3 `evolve/adapter.py` (③ 솔버 hooks)

```python
"""<solver>-bench solver hooks."""

SOLVER_NAME = "<solver>"

# problems.jsonl 필드 매핑
PROBLEM_FILE_FIELD = "<solver>_filename"      # 문제 파일명 키
STATUS_FIELD = "<solver>_status"              # nested {"result", "elapsed_ms", ...}
STATS_FIELD = "<solver>_response_stats"       # None ok (baseline에 stats 없을 때)
FEATURES_FIELD = "features"
OBJECTIVE_FIELD = "objective_value"           # STATUS_FIELD 내부 path; None ok

# 결과 분류
DECISIVE_RESULTS = ("Sat", "Unsat")           # solver가 "확실히 풀었다"는 결과
DECIDED_RESULTS  = ("Sat", "Unsat")           # regression 판정 기준

# 평가 metric에 surface할 solver 카운터
KEY_STATS = ("conflicts", "decisions", "propagations")

# Efficiency factor의 stat key별 weight
STATS_WEIGHTS = {
    "conflicts": 2.0,
    "decisions": 1.5,
    "propagations": 0.5,
}

# Score mode default (config.yaml evaluation.score_mode가 override)
SCORE_MODE = "speedup"

# Worker count axis가 있는 솔버이면 키 이름, 없으면 None
WORKERS_KEY = None                            # cpsat: "num_search_workers"


def get_problem_size(features):
    """Clustering feature 추출. 어떤 값으로 문제를 클러스터링할지."""
    return int((features or {}).get("num_constraints") or 0)
```

### 2.4 `evolve/_solve_worker.py` (④ subprocess entry)

`_lib.subprocess_runner.run_solver`가 호출함. argv 계약:
```
argv[1]  JSON string of params dict
argv[2]  problem file path
argv[3]  timeout_s (int)
```

stdout: 마지막 비어 있지 않은 줄에 JSON 결과 한 개.

success:
```json
{"result": "Sat", "elapsed_ms": 1234, "stats": {"conflicts": 100, ...}, "objective": 42.0}
```

invalid param:
```json
{"invalid_param": "<key>", "error": "<msg>", "result": "Unknown", "elapsed_ms": 0}
```

크래시:
```json
{"result": "Unknown", "elapsed_ms": 0, "error": "<msg>"}
```

timeout은 `_lib.subprocess_runner`가 처리하므로 worker는 신경 안 써도 됨.

템플릿 (Python solver binding 가정):
```python
import json
import sys
import time


def emit(d):
    print(json.dumps(d))
    sys.stdout.flush()


def main():
    if len(sys.argv) != 4:
        emit({"result": "Unknown", "elapsed_ms": 0, "error": "bad argv"})
        return

    params_json, problem_path, timeout_s = sys.argv[1], sys.argv[2], int(sys.argv[3])
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError as e:
        emit({"result": "Unknown", "elapsed_ms": 0, "error": f"params parse: {e}"})
        return

    # 1. 솔버 instance 생성 + params 적용
    try:
        import <solver_pkg>
        solver = <solver_pkg>.Solver()
        for k, v in params.items():
            solver.set_param(k, v)
    except <solver_pkg>.InvalidParamError as e:
        emit({"invalid_param": str(e), "error": str(e),
              "result": "Unknown", "elapsed_ms": 0})
        return

    # 2. 문제 로드
    with open(problem_path) as f:
        solver.parse(f.read())

    # 3. 실행
    t0 = time.monotonic()
    try:
        result = solver.solve(timeout_s)
    except TimeoutError:
        emit({"result": "Unknown", "elapsed_ms": int((time.monotonic() - t0) * 1000),
              "timeout": True, "stats": {}})
        return
    except Exception as e:
        emit({"result": "Unknown", "elapsed_ms": int((time.monotonic() - t0) * 1000),
              "error": str(e), "stats": {}})
        return

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    emit({
        "result": result,                   # "Sat" / "Unsat" / "Unknown" / "OPTIMAL" / ...
        "elapsed_ms": elapsed_ms,
        "stats": solver.statistics(),       # dict of numeric counters
        "objective": solver.objective(),    # optional
    })


if __name__ == "__main__":
    main()
```

CLI 솔버 (binary)면 `subprocess.run([solver_binary, ...])` 호출 후 stdout 파싱.

---

## 3. Phase 모듈 작성

`evolve/phase{N}_<name>/initial_program.py` 1개씩.

### 3.1 단순 phase (z3-style — flat overrides)

```python
"""
Phase 1: tune <solver>'s <namespace> knobs.

Targeted namespace: <key1>, <key2>, <key3>.
Other params stay at BASELINE.

Do NOT modify locked keys.
"""
import os
import pathlib
import sys


def _resolve_bench_root():
    v = os.environ.get("OPENEVOLVE_BENCH_ROOT")
    if v:
        return pathlib.Path(v).resolve()
    here = pathlib.Path(__file__).resolve()
    for p in [here.parent.parent] + list(here.parents):
        if (p / "params.json").exists() and (p / "adapter.py").exists():
            return p
    raise RuntimeError("OPENEVOLVE_BENCH_ROOT unset")


_BENCH = _resolve_bench_root()
_INPUT = _BENCH.parent.parent
if str(_INPUT) not in sys.path:
    sys.path.insert(0, str(_INPUT))

from _lib import params_catalog  # noqa: E402

BASELINE = params_catalog.load_for_bench(_BENCH).defaults


# EVOLVE-BLOCK-START
OVERRIDES = {}
# EVOLVE-BLOCK-END


def get_params():
    p = dict(BASELINE)
    p.update(OVERRIDES)
    return p


def get_phase_overrides():
    return dict(OVERRIDES)
```

### 3.2 cpsat-style phase (SIZE_BUCKETS + STAGE3_OVERRIDES + worker lock)

`config.yaml`의 `evaluation.enable_size_buckets: true`일 때만:

```python
import os, pathlib, sys
# ... _resolve_bench_root + BASELINE 동일 ...

PHASE_LOCKED = {
    "<seed_key>": 0,
    "<worker_key>": 1,           # adapter.WORKERS_KEY와 일치
}


# EVOLVE-BLOCK-START
GLOBAL_OVERRIDES = {}
SIZE_BUCKETS = [
    (50_000,       {}),
    (150_000,      {}),
    (float("inf"), {}),
]
STAGE3_OVERRIDES = {}
# EVOLVE-BLOCK-END


def _bucket_override(size):
    for upper, override in SIZE_BUCKETS:
        if size < upper:
            return override
    return {}


def get_params(problem=None, stage=None):
    p = dict(BASELINE)
    p.update(GLOBAL_OVERRIDES)
    if problem is not None:
        p.update(_bucket_override(int(problem.get("size") or 0)))
        if stage == "stage3" and problem.get("is_outlier"):
            p.update(STAGE3_OVERRIDES)
    p.update(PHASE_LOCKED)
    return p


def get_phase_overrides():
    return dict(GLOBAL_OVERRIDES)


def get_phase_size_buckets():
    return [(u, dict(d)) for u, d in SIZE_BUCKETS]


def get_phase_stage3_overrides():
    return dict(STAGE3_OVERRIDES)
```

### 3.3 Unified phase (마지막 phase)

```python
"""
Unified refinement — EVOLVE-BLOCK 자동 머터리얼됨.
prepare_phase가 phase{1..N-1}_best.json union으로 채움.
"""
# ... 동일한 prelude ...

# EVOLVE-BLOCK-START
UNIFIED_OVERRIDES = {}   # 자동 채워짐 — config.yaml의 unified_dict_name과 일치
# EVOLVE-BLOCK-END

def get_params():
    p = dict(BASELINE)
    p.update(UNIFIED_OVERRIDES)
    return p

def get_phase_overrides():
    return dict(UNIFIED_OVERRIDES)
```

### 3.4 Phase 모듈 핵심 contract

| 항목 | 필수 / 선택 | 용도 |
|---|---|---|
| `BASELINE` | 필수 | `params_catalog.load_for_bench(_BENCH).defaults` |
| `PHASE_LOCKED` | 선택 (worker 차등 시 필수) | 평가 시 LOCK 강제 |
| `EVOLVE-BLOCK-START/END` 마커 | 필수 | LLM mutation 범위 + prepare_phase target |
| `get_params(problem=None, stage=None)` | 필수 | 평가자가 호출하는 entry point |
| `get_phase_overrides()` | 필수 (마지막 phase 제외) | extract_best가 dict 추출 |
| `get_phase_size_buckets()` | 선택 (enable_size_buckets 시) | SIZE_BUCKETS 추출 |
| `get_phase_stage3_overrides()` | 선택 (enable_outlier_stage 시) | STAGE3_OVERRIDES 추출 |

---

## 4. 동작 검증 절차

```bash
# 0. 솔버 binding 설치 확인
python3 -c "import <solver_pkg>; print(<solver_pkg>.__version__)"

# 1. catalog 로드 + validation 확인
python3 -c "
from _lib import params_catalog
c = params_catalog.load('input/<solver>-bench/evolve/params.json')
print('keys:', len(c.known_keys()), 'defaults:', len(c.defaults), 'locked:', len(c.locked))
print('validate ok:', c.validate(c.defaults))
print('validate bogus:', c.validate({'fake_key': 1}))
"

# 2. sampler — clustering + stage 분할
python3 -m _lib.sampler <solver>-bench
# Expect: cache/stage{1..4}_sample.json 생성

# 3. self_test — BASELINE으로 stage1 1회 평가
python3 -m _lib.self_test <solver>-bench
# Expect: 결과 라벨 매치 + ratio [0.5, 2.0] 권장 (WARN ok)

# 4. rebaseline — 로컬 baseline 캡쳐 (10회 평균)
python3 -m _lib.rebaseline <solver>-bench
# Expect: cache/local_baseline.json 생성

# 5. 1 phase smoke run (적은 iter로)
./input/run_phase.sh <solver>-bench 1 --pin 2-3
# Expect: phase1/openevolve_output/best/best_program.py 생성
#         cache/phase1_best.json 생성

# 6. 전체 phase chain
./input/run_phase.sh <solver>-bench --pin 2-7
# Expect: final_program.py 생성
```

---

## 5. 옵션 결정 가이드

### 5.1 `score_mode` 선택

| 솔버 특성 | 추천 mode |
|---|---|
| baseline에 objective_value 있음, 최적화 문제 | `cost` |
| Sat/Unsat 만족도 + wall-clock 최소화 | `speedup` |
| Determinism work counter (예: cpsat의 deterministic_time) 있음 | `cost` + `time_metric: dtime` |

### 5.2 Worker 축 있는 솔버?

`PHASE_LOCKED["num_workers"] = N` 같은 phase별 차등 운영하면:
- adapter에 `WORKERS_KEY = "<key>"` 명시
- evaluator_core가 core block alloc 사용
- rebaseline이 `by_workers` 스키마 사용

없으면:
- `WORKERS_KEY = None`
- 1 core per solve 단순 배분

### 5.3 SIZE_BUCKETS / STAGE3_OVERRIDES 활성?

| 상황 | enable? |
|---|---|
| 문제 크기 분포 넓음 (예: ~7k–250k constraints, 점수 분포 multi-modal) | `enable_size_buckets: true` |
| 일부 outlier 문제가 score를 dominate | `enable_outlier_stage: true` + `cache/outliers.json` 채우기 |
| Pool 작거나 균일 | 둘 다 `false` |

### 5.4 `clustering.method`

| Method | 용도 |
|---|---|
| `kmeans` | 1D Lloyd's. 자연스러운 cluster boundary 자동 발견 |
| `quintile` | rank 기반 균등 분할. boundary 일관성 중요할 때 |
| `thresholds` | 사용자가 명시한 cut-off (`[50000, 150000]` → 3 bucket) |

---

## 6. 흔한 함정

1. **`problems.jsonl` 필드명 mismatch** — adapter의 `PROBLEM_FILE_FIELD` /
   `STATUS_FIELD`가 실제 JSON 키와 정확히 일치해야 함. typo 흔함.

2. **`features.<feature>` 누락** — `clustering.feature: features.num_X`인데
   problems.jsonl에 `features.num_X` 없으면 sampler가 0으로 처리 → 모든 문제
   1개 cluster로 뭉침.

3. **DECISIVE vs DECIDED 혼동** — DECISIVE는 "솔버가 답을 줬다" (Sat/Unsat),
   DECIDED는 "베이스라인이 확정 답을 줬으니 regression 비교 가능". 대부분의
   솔버는 두 셋 동일하지만 cpsat은 다름 (FEASIBLE은 decisive지만 INFEASIBLE은
   decided에만).

4. **`_solve_worker.py`에서 invalid param 미감지** — 솔버가 unknown key를
   silently ignore하면 catalog만으로 검증 안 됨. worker가 명시적으로
   `{"invalid_param": "<key>"}` 출력해야 evaluator가 0점 부여.

5. **Phase docstring 비워둠** — LLM이 phase intent 파악하는 유일한 채널.
   "Phase X: tune <namespace>" 한 줄이라도 적어두면 mutation 품질 차이 큼.

6. **`unified_dict_name` 불일치** — config.yaml `bench.unified_dict_name`과
   마지막 phase initial_program.py의 EVOLVE-BLOCK 내 dict 이름이 일치해야
   prepare_phase가 머터리얼 가능.

7. **`worker_path` 경로** — config.yaml `bench.worker_path`는 `<bench>/evolve/`
   기준 상대 경로. `_solve_worker.py`가 evolve/ 루트에 있으면 그냥
   `_solve_worker.py` (디렉토리 prefix 없이).

---

## 7. 작업 시간 견적

| 항목 | 예상 |
|---|---|
| raw-data 수집 + baseline 실행 (사용자 측 작업) | 가변 (수 시간 ~ 며칠) |
| problems.jsonl 생성 스크립트 작성 | 30분 - 1시간 |
| config.yaml 작성 | 30분 |
| params.json 작성 (50개 키 catalog) | 1-2시간 |
| adapter.py 작성 | 10분 |
| _solve_worker.py 작성 | 30분 - 2시간 (binding 복잡도에 따라) |
| phase 모듈 N개 작성 | 30분 (phase당 10분 × N) |
| 검증 + 디버깅 | 1-3시간 |
| **합계** | **반나절 - 1일** |

---

## 8. 참고 — 기존 솔버 사례

| Solver | Score mode | Worker axis | Size buckets | Phases |
|---|---|---|---|---|
| z3 (`z3-bench`) | speedup | NO | NO | 4 (opt/sls + sat + smt + unified) |
| CP-SAT (`cpsat-bench`) | cost (dtime + cost_ratio) | YES (W=1, W=8) | YES | 5 (search + presolve + lp_cuts + unified + custom_subsolvers) |

두 bench의 `params.json`, `adapter.py`, phase 모듈을 템플릿으로 참고.
