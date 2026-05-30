---
name: tennis-scheduling-algorithm
description: 테니스 대진표 생성 알고리즘. 슬롯×코트 매트릭스에 게임을 배정하고 (남복/여복/혼복), 페어 중복 회피, 게임수 균형, 연속 출전 회피, 구력 밸런스를 동시 최적화. 생성 결과의 품질도 평가.
---

# Tennis Scheduling Algorithm

(시간슬롯 × 코트) 매트릭스에 매치를 배정하는 휴리스틱 알고리즘과 품질 평가기.

## 알고리즘 개요

**다중 시드 그리디 + 점수 기반 후보 선택.**

1. R회 (기본 80회) 반복:
   - 매번 다른 랜덤 시드로 슬롯×코트 셀을 순서대로 채움
   - 각 셀에서 가용 풀의 4명 후보 조합을 K개 (기본 24개) 평가 → 최고점 선택
   - 전체 결과의 총점 계산
2. R개 결과 중 총점 최저(=최선) 선택

## 점수 함수 (낮을수록 좋음)

각 매치의 비용 + 전체 결과의 비용 합산:

### 매치 단위 비용 (composer가 후보 선택 시 사용)
```
match_cost =
    w_team_skill_diff * |sum(team1_exp) - sum(team2_exp)|
  + w_pair_repeat     * (이 게임 페어 중 이미 출현한 페어 수 * 페어 빈도)
  + w_consecutive     * sum(직전 슬롯에서 이 사람이 게임했는지) 인원수
  + w_three_consec    * sum(직전2슬롯 연속 출전 여부) * 100
  + w_game_balance    * sum(이 게임 참가자의 현재 게임수 - 평균 게임수)^2
  + w_mixed_overuse   * (남복/여복이 가능한데 혼복을 골랐을 때 페널티)
  + w_mixed_skill_rule_violation * (혼복에서 남자 구력 < 여자 구력일 때 큰 페널티)
  + w_no_member_guest_mix * (한 팀이 전원 정회원 또는 전원 게스트일 때 작은 페널티)
```

### 가중치 (기본값, schedule.py에서 조정 가능)
```python
W = dict(
    team_skill_diff=2.0,
    pair_repeat=15.0,
    consecutive=3.0,
    three_consec=50.0,
    game_balance=1.5,
    mixed_overuse=8.0,
    mixed_skill_rule_violation=100.0,
    no_member_guest_mix=1.0,
)
```

## 후보 생성 전략 (composer가 각 셀에서)

가용 풀이 N명일 때 C(N,4) 전체를 다 평가하면 폭주. 그래서:

1. 가용 풀 정렬: (게임수 적은 순, 직전 슬롯 미출전 순, 무작위 jitter)
2. 상위 min(N, 10)명만 후보로 → C(min(N,10), 4) ≤ 210 평가
3. 게임 유형별 가능성:
   - 남자 ≥ 4명 → 남복 후보 생성
   - 여자 ≥ 4명 → 여복 후보 생성
   - 남자 ≥ 2 AND 여자 ≥ 2 → 혼복 후보 생성
4. 4명 조합에 대해 가능한 팀 분할:
   - 남복/여복: 3가지 분할 (1-2 vs 3-4, 1-3 vs 2-4, 1-4 vs 2-3)
   - 혼복: 남자 2명 / 여자 2명 고정 → 페어 짝 2가지 (남1+여1 / 남2+여2, 또는 남1+여2 / 남2+여1)
5. 모든 (후보 조합, 분할) 쌍에 대해 match_cost 계산 → 최저 선택

## 슬롯 처리 순서

기본: 시간 오름차순. 동일 시간 내 코트는 알파벳 순.

시간 진행 시 "지나간 슬롯의 결과"를 현재 비용 계산에 반영 (페어 중복, 게임수, 연속).

## 사용

### 대진 생성
```powershell
python C:\Works\auto-tennis-bracket\.claude\skills\tennis-scheduling-algorithm\scripts\schedule.py `
  --in  C:\Works\auto-tennis-bracket\_workspace\01_parsed.json `
  --out C:\Works\auto-tennis-bracket\_workspace\02_bracket.json `
  --seed 42 --iters 80 --candidates 24
```

### 품질 평가
```powershell
python C:\Works\auto-tennis-bracket\.claude\skills\tennis-scheduling-algorithm\scripts\review.py `
  --parsed  C:\Works\auto-tennis-bracket\_workspace\01_parsed.json `
  --bracket C:\Works\auto-tennis-bracket\_workspace\02_bracket.json `
  --out     C:\Works\auto-tennis-bracket\_workspace\03_review.json
```

review.py는 평가표를 stdout에 출력하고 verdict(PASS/RETRY/ACCEPT_WITH_WARNINGS)를 결정.

## 임계값 (review.py)

| 지표 | 임계값 (PASS) | 초과 시 |
|------|--------------|---------|
| 동일시간대 게임수 격차 | ≤ 2 | RETRY |
| 전체 게임수 격차 | ≤ 4 | RETRY |
| 페어 중복률 | ≤ 5% | RETRY |
| 3슬롯 연속 출전 | = 0 | RETRY (절대 임계값) |
| 평균 팀 구력 차이 | ≤ 3 | RETRY |
| 최대 팀 구력 차이 | ≤ 5 | RETRY |
| 혼복 규칙 위반 (남<여 구력) | = 0 | RETRY |
| 혼복 비율 (성별 균형 시) | ≤ 25% | WARNING (RETRY 아님) |

3회 RETRY 후에도 통과 못하면 ACCEPT_WITH_WARNINGS로 전환.

## 알고리즘 한계 (사용자 보고 항목)

- 가용 인원이 정말 부족한 슬롯은 공석 처리 (예: 09:00 슬롯에 2명만 가능)
- 남자 또는 여자가 극단적으로 적으면 혼복 비율 임계값 무시
- 게임수 격차는 IN/OUT 차이가 큰 인원이 섞이면 불가피하게 커짐

이런 한계는 review가 ACCEPT_WITH_WARNINGS와 함께 명시적으로 보고.
