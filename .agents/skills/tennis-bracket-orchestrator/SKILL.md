---
name: tennis-bracket-orchestrator
description: 테니스 클럽 주간 대진표를 자동 생성한다. "테니스 대진표", "대진 짜줘", "테니스 스케줄", "코트 배정", "참가자 명단으로 대진" 등의 요청에 반드시 이 스킬을 사용. 입력 엑셀 템플릿 만들기 / 채워진 엑셀로 결과 대진표 생성 / 기존 대진표 부분 수정 / 재실행 / 보완 / 업데이트 모두 처리.
---

# Tennis Bracket Orchestrator

테니스 대진표 생성을 위한 4-에이전트 파이프라인 오케스트레이터. 입력 엑셀 템플릿 생성과 채워진 입력으로부터의 대진표 생성을 모두 처리.

## 실행 모드

**에이전트 팀 모드 (기본)**

`requirements-parser → bracket-composer ⇄ bracket-reviewer → excel-renderer` 파이프라인.
composer ⇄ reviewer 사이는 피드백 루프(최대 3회).

오버헤드를 피하기 위해 **단일 작업(예: 템플릿만 생성)일 때는 서브 에이전트로 단축 실행**한다.

## Phase 0: 컨텍스트 확인 (반드시 먼저 실행)

요청을 받으면 가장 먼저 작업 디렉토리에서 다음을 확인:

1. `_workspace/01_parsed.json` 존재 여부
2. `_workspace/02_bracket.json` 존재 여부
3. 사용자 요청이 (a) 신규 / (b) 부분 수정 / (c) 재실행 / (d) 템플릿만 생성 / (e) 입력 파일 받음 중 어느 것인지

판정 매트릭스:

| 사용자 요청 | _workspace 상태 | 실행 모드 |
|------------|----------------|-----------|
| "입력 양식 만들어줘" / "템플릿" | 무관 | **Phase A만** (excel-renderer 단독) |
| 채워진 입력 엑셀 경로 제공 | 무관 | **Phase B 전체** (4-에이전트 팀) |
| "다시 만들어", "재실행" | 02_bracket.json 있음 | 기존 → `_workspace_prev/`로 이동, **Phase B 전체** (새 시드) |
| "8번 게임만 바꿔" 등 부분 수정 | 02_bracket.json 있음 | **부분 재실행** (해당 슬롯만 composer 재호출) |
| "특정 사람 추가됐어" | 02_bracket.json 있음 | parser → composer만 재실행 |
| 처음 / `_workspace/` 없음 | 없음 | 사용자에게 입력 방식 확인 후 분기 |

## Phase A: 입력 템플릿 생성 (단축 워크플로우)

사용자가 "입력 양식만 줘"라고 하면:

1. `excel-renderer` 에이전트 1명만 호출 (서브 에이전트 모드)
2. 호출 시 `model: "opus"`, `subagent_type: "general-purpose"` 명시
3. 출력 경로 확인 (기본: `C:\Works\auto-tennis-bracket\테니스_입력양식.xlsx`)
4. `tennis-input-template/scripts/build_template.py` 실행. `--prefill image` 옵션 사용 시 첨부 이미지에서 추출 가능한 데이터 사전 채움.
5. 결과 파일 경로를 사용자에게 보고 + 작성 가이드 안내

작성 가이드 메시지 (사용자에게 보낼 것):
```
입력 템플릿이 생성됐어요. 시트 두 개를 채워서 다시 요청하세요.

📄 `참가자` 시트
  - 이름, 성별(남/여), 구력(년수, 정수), 구분(정회원/게스트)
  - IN시간, OUT시간 (HH:MM, 30분 단위)

📄 `코트` 시트
  - 코트명, 시작시간, 종료시간 (HH:MM)
  - 기본값으로 A, B (08:00~12:00), C (07:00~09:00) 채워져 있음 — 필요시 수정

채우신 후 "이 파일로 대진표 만들어줘" 라고 해주세요.
```

## Phase B: 대진표 생성 (4-에이전트 팀)

채워진 입력 엑셀이 제공되면:

### Step 1: 팀 구성
```
TeamCreate (team_name: "bracket-team", members: [
  requirements-parser, bracket-composer, bracket-reviewer, excel-renderer
])
```

### Step 2: 작업 할당 (TaskCreate, 의존 관계 명시)
1. `T1: 입력 파싱` → owner: requirements-parser
2. `T2: 대진 생성` → owner: bracket-composer, blockedBy: [T1]
3. `T3: 품질 검토` → owner: bracket-reviewer, blockedBy: [T2]
4. `T4: 엑셀 출력` → owner: excel-renderer, blockedBy: [T3]

### Step 3: 데이터 흐름 (파일 기반)
모든 중간 산출물은 `C:\Works\auto-tennis-bracket\_workspace\` 하위에 저장:
- `01_parsed.json` (parser 출력)
- `02_bracket.json` (composer 출력, 재시도 시 덮어쓰기)
- `03_review.json` (reviewer 판정)
- 최종 엑셀은 작업 디렉토리에 `테니스_대진표_YYYYMMDD.xlsx` (YYYYMMDD는 사용자에게 확인 또는 입력 데이터의 날짜)

### Step 4: 재시도 루프
reviewer가 RETRY 판정 → composer가 retry_hint를 받고 시드를 변경하여 재실행 → 최대 3회.
3회 후에도 RETRY면 reviewer는 ACCEPT_WITH_WARNINGS로 전환하고 사용자에게 한계를 보고.

### Step 5: 사용자 보고
완료 시 다음을 메시지로 출력:
- 결과 엑셀 경로
- 참가자 게임수 요약 (max, min, 평균)
- 페어 중복 비율, 혼복 비율
- 경고 사항 (있다면)

## 데이터 전달 프로토콜
- **태스크 기반** (조율): TaskCreate/TaskUpdate로 진행 추적
- **파일 기반** (산출물): `_workspace/` 하위 JSON 파일
- **메시지 기반** (재시도): composer ⇄ reviewer SendMessage로 retry_hint 전달

## 에러 핸들링
| 에러 유형 | 처리 |
|----------|------|
| 입력 엑셀 파싱 실패 | parser가 즉시 사용자에게 어느 행/컬럼이 문제인지 보고 후 중단 |
| 가용 인원 < 4명인 슬롯 존재 | composer가 해당 슬롯 공석 처리, reviewer가 보고서에 명시 |
| reviewer 3회 RETRY 후에도 임계값 미달 | ACCEPT_WITH_WARNINGS로 진행, 보고서에 위반 항목 나열 |
| 엑셀 출력 실패 (파일 잠김 등) | excel-renderer가 사용자에게 파일 닫고 재실행 요청 |

## 코트별 배정 규칙

알고리즘이 따르는 코트 affinity (`schedule.py`의 `COURT_AFFINITY`):
- **A코트**: 여복 + 혼복 우선 (남복 페널티)
- **B코트**: 남복 우선 (여복/혼복 페널티)
- **C코트**: 무관 (모든 유형 균등)

여성 참가자는 07:30 이전 슬롯에는 가급적 배정하지 않음 (IN 시간이 7시 이전인 여성이 있다면 예외).

이 규칙은 score 가중치(`W["court_affinity"]`, `W["female_early_slot"]`)로 표현되며, 인원·코트 제약상 강제 불가능한 경우 자연스럽게 양보.

## 테스트 시나리오

**정상 흐름:**
- 입력: 첨부 이미지의 10명 참가자, 코트 A/B/C (A,B: 08-12, C: 07-09)
- 기대: 30분 단위 10개 슬롯, 코트별로 대진 배정, 참가자별 게임수 4~7회 범위
- 검증: 페어 중복 ≤ 5%, 연속 3슬롯 = 0, 혼복 비율 ≤ 20%

**에러 흐름:**
- 입력: 참가자 3명만 있음
- 기대: parser가 "최소 4명 필요" 보고 + 진행 중단
- 검증: 결과 엑셀 미생성, 사용자에게 명확한 에러 메시지

## 후속 작업 지원

- **"다시" / "재실행" / "다른 패턴"**: `_workspace/`를 `_workspace_prev/`로 이동, 시드 변경 후 전체 재실행
- **"X번 게임만 수정해줘"**: composer만 호출, 해당 슬롯의 매치만 재계산 (인접 슬롯 영향 분석)
- **"참가자 추가/제거"**: parser부터 다시 실행
- **"코트 시간 변경"**: parser부터 다시 실행
- **"결과 양식 색깔/폰트 수정"**: excel-renderer만 호출

각 에이전트는 정의 파일의 "후속 작업" 섹션에 따라 동작한다.
