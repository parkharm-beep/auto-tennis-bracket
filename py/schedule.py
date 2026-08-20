"""Generate a tennis bracket via multi-seed greedy heuristic + swap local search.

Usage:
    python schedule.py --in <parsed.json> --out <bracket.json> [--seed 42] [--iters 80] [--candidates 24]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import sys
from functools import lru_cache

# ---------------------------------------------------------------------------
# W: 그리디 후보 선택용 가중치 (한 경기를 고를 때의 비용)
# ---------------------------------------------------------------------------
W = dict(
    team_skill_diff=4.0,
    skill_gap_over_tol=40.0,    # 두 팀 구력합 차이가 허용치 초과 (초과분)^2
    pair_repeat=20.0,
    consecutive=3.0,            # 2슬롯 연속은 '몰아서 하고 일찍 끝내기'에 유리 → 약하게만 억제
    three_consec=500.0,
    consecutive_banned=100000.0, # 개인 '연속게임=금지': 2연속 하드필터 이중 안전장치
    three_consec_allowed=20.0,   # 개인 '연속게임=허용': 3연속까지만 (4연속은 three_consec)
    game_balance=3.0,
    mixed_overuse=5.0,
    mixed_skill_rule_violation=1000.0,
    no_member_guest_mix=1.0,
    court_affinity=25.0,
    mixed_nonpriority=120.0,    # 교류전: 단성복식(남복/여복) 우선 — 단, 여복 페어가 반복되면 혼복도 허용
    women_doubles_bonus=200.0,  # 교류전: 여복 우대(우선) — 양 클럽 여자 2+면 여복을 먼저, 다만 절대적이지 않아 혼복도 가능
    history_pair_repeat=30.0,   # 지난주/2주전과 같은 페어 회피(소프트). 우리멤버끼리(단일클럽)일 때만 적용, 교류전 제외.
                                # 같은주 중복페어 유효비용(pair_repeat 20 → 첫 중복 40)보다 낮게 둔다:
                                # 지난주 회피 때문에 이번 주 안에서 같은 짝을 반복하는 자기모순을 막기 위함.
    quad_repeat=95.0,           # 같은 4명이 편만 바꿔 다시 만남 — 이것도 확실히 회피
    quad_repeat_forced=12.0,    # 단, 그 성별로 만들 '새 4명 조합'이 아예 없을 때(예: 여자 4명)는
                                # 편을 바꾼 재대결이 유일한 단성 복식이다. 억지로 혼복으로 밀지 않도록
                                # 가볍게만 억제한다 (혼복 페널티 single_mixed_nonpriority=70보다 훨씬 작게).
    matchup_repeat=90.0,        # 같은 4명이 '상대편까지 그대로' 다시 만남 (강하게)
    mixed_below_min=40.0,       # 최소 보장 판수를 못 채운 동안 혼복 우대 (음수 비용)
    single_mixed_nonpriority=70.0,
    # ↑ 평소(단일 클럽)에도 남복/여복 우선. 혼복은 '단성 복식이 가능한데도' 고를 때만 페널티.
    #   페어 중복 첫 발생 비용(pair_repeat 20 → 40)보다 크게 두어, 페어가 한 번 겹치는 정도는
    #   혼복보다 낫다고 보고, 두 번 이상 겹칠 상황(20*(4+1)=100)이면 혼복을 차선책으로 허용한다.
    gender_catchup=25.0,        # 평균보다 뒤처진 성별을 먼저 투입 (뒤처진 게임수 1당, 음수 비용)
    idle_urgency=14.0,
    # ↑ 오래 쉰 사람을 먼저 투입(음수 비용). '1시간 이상 공백'을 만들기 전에 되돌리는 힘.
    #   쉰 시간이 길수록 커지므로 한 번 밀린 사람이 계속 밀리는 악순환이 생기지 않는다.
    min_games_deficit=35.0,     # 최소게임수 미달자를 먼저 투입 (부족 게임수 1당, 음수 비용)
    min_games_critical=600.0,   # 남은 가용 슬롯이 부족분과 같으면 지금 무조건 태워야 함 (음수 비용)
    couple_avoid_pair=250.0,    # '피함' 부부가 혼복 같은 팀 — 강하게 회피 (인원상 불가피하면 양보)
    couple_want_pair=40.0,      # '원함' 부부가 혼복 같은 팀 — 우대 (음수 비용). 혼복 자체를 늘리진 않게 약하게
    guest_in_mixed=30.0,        # 남자 게스트는 혼복보다 남복 위주 — 혼복에 남자 게스트 1명당 페널티.
                                # (여자 게스트는 혼복 가능 — 페널티 없음)
                                # 혼복을 막는 게 아니라 '혼복 남자 자리를 정회원이 맡게' 미는 힘이므로
                                # single_mixed_nonpriority(70)보다 낮게 둔다.
    mixed_wish_seat=55.0,       # 혼복희망을 아직 못 채운 사람이 이 혼복에 있음 (1명당, 음수 비용).
                                # guest_in_mixed(30)를 이겨야 남자 게스트 희망자가 혼복 자리를 잡는다.
                                # 충족된 뒤에는 붙지 않으므로 과충족 압력이 없다.
)

IDLE_URGENCY_CAP = 4.0          # 유휴 우선 보정 상한(30분 단위) — 2시간 이상은 더 커지지 않음

# 후보 조합 상한 (게임수 적은 순 정렬 기준 상위 N명). 조합 폭발을 막아 속도를 확보한다.
SINGLES_TOP = 8                 # 남복/여복 조합에 쓸 인원
MIXED_TOP = 5                   # 혼복 조합에 쓸 남/여 인원

# ---------------------------------------------------------------------------
# G: 완성된 대진표 전체를 평가하는 가중치 (시드 선택 + 로컬 개선의 목적함수)
# ---------------------------------------------------------------------------
G = dict(
    balance_sq=12.0,
    balance_spread=30.0,
    balance_under=400.0,        # 공평 기준(내림)보다도 덜 뛰는 사람 (부족분)^2 — 사실상 금지
    balance_short=170.0,        # 공평 기준에 0.5게임 넘게 못 미치는 사람 (초과분)^2
    gender_fairness=420.0,      # 한쪽 성별 평균이 전체 평균보다 낮게 굳는 것을 방지 (초과분)^2
    # ↑ 여자가 4명뿐이면 여복은 '4명 통째'로만 늘어나므로, 개인 단위 균형만으로는
    #   "부족분 몫을 항상 여자 4명이 진다"가 비용상 동점이 되어 그대로 채택된다.
    #   성별 그룹 평균까지 보게 해서 그 쏠림을 깬다.
    pair_dup=55.0,
    quad_repeat=210.0,          # 같은 4명이 편만 바꿔 재대결 — 그럴 바엔 혼복이 낫다
    quad_repeat_forced=25.0,    # 단, 그 성별의 4명 조합을 이미 다 써버렸다면(예: 여자 4명 → 조합 1개)
                                # 재대결은 구조적으로 불가피하다. 이때 여복을 막으면 혼복만 남으므로
                                # 가볍게만 문다 (혼복 1판 mixed_match=20과 비슷한 급).
    matchup_repeat=220.0,       # 같은 4명이 상대편까지 그대로 재대결 — 혼복 1~2판보다 나쁘다고 본다
    history_pair=30.0,
    three_streak=100000.0,      # 3연속 출전 금지(하드) — "2게임 연속했으면 반드시 쉰다".
                                # 그리디는 애초에 안 만들고(safe_pool·하드 필터, 예외 없음),
                                # 로컬 개선(선수 교환·경기 이동·kick)도 이 비용 때문에 절대 못 만든다.
                                # 공백 해소(long_gap)·최소게임수(min_games_short)와도 안 바꾼다.
    two_streak=2.0,
    two_streak_banned=100000.0, # 개인 '연속게임=금지': 2연속 사실상 하드
    three_streak_allowed=40.0,  # 개인 '연속게임=허용': 3연속만 소프트 (4연속부터 three_streak 하드)
    mixed_match=20.0,           # 허용량 이내의 혼복 1경기당 페널티
    mixed_over_quota=220.0,     # 허용량을 넘는 혼복 (초과 경기수)^2 — 사실상 상한
    mixed_below_min=300.0,      # 최소 보장 판수에 못 미치는 혼복 (부족 경기수)^2 — 사실상 하한
    # ↑ 혼복 억제의 주 장치는 그리디의 single_mixed_nonpriority(중복 없는 단성 복식이
    #   가능할 때만 부과)다. 여기서 과하게 잡으면 '단성 복식이 더 못 나오는 상황'에서도
    #   혼복이 막혀 여자 게임수가 깎이므로 낮게 둔다.
    women_doubles_bonus=100.0,  # 교류전: 여복 1경기당 보너스
    team_skill_diff=4.0,
    skill_gap_over_tol=40.0,    # 두 팀 구력합 차이가 허용치 초과 (초과분)^2
    mixed_skill_violation=1000.0,
    court_affinity=10.0,
    member_guest=1.0,
    gap=4.0,                    # 경기 사이 공백 30분당
    long_gap=200.0,             # 경기 사이 공백이 1시간 이상일 때 (초과 30분 단위)^2 — 최우선 회피
    # ↑ 같은 4명 재대결(quad_repeat)보다 위. 한 사람이 1시간 노는 게 대진 한 판 겹치는 것보다 나쁘다.
    start_delay=5.0,            # 도착(IN) 후 첫 경기까지 대기 30분당
    long_start_delay=60.0,      # 첫 경기까지 1시간 넘게 기다릴 때 (초과 30분 단위)^2
    idle_sq=5.0,                # 개인별 총 유휴시간(대기+공백)의 볼록 페널티 — 한 사람에게 몰리는 것 방지
    missed=5000.0,
    seed_missing=20000.0,       # 씨드(고정 배치) 자리를 못 채운 것 — 자리당.
                                # 초안 40개 중 '씨드를 지킨 안'이 뽑히게 하는 항이다.
                                # (Refiner는 씨드를 못 건드리므로 여기서만 의미가 있다.
                                #  씨드가 없으면 0이라 기존 동작과 완전히 같다)
    min_games_short=900.0,      # 최소게임수 미달 (부족분)^2 — 어기지 않는 규칙. balance_under(400)보다 위
    guest_in_mixed=25.0,        # 혼복에 남자 게스트 1명당 — 혼복 남자 자리는 가급적 정회원이 (소프트)
    mixed_wish_short=350.0,     # 혼복희망 미달 (부족 게임수)^2. min_games_short(900)보다는 아래 —
                                # '최소게임수 보장'과 부딪히면 최소게임수가 이긴다
    couple_avoid_pair=250.0,    # '피함' 부부가 혼복 같은 팀 (경기당)
    couple_want_pair=30.0,      # '원함' 부부가 혼복 같은 팀 (경기당 보너스, 음수)
    couple_finish_gap=120.0,    # 부부 마지막 경기 종료 차이가 30분을 넘으면 (초과 30분 단위)^2
                                # 부부는 같이 오가므로 같이 끝나거나 30분 안쪽 차이가 목표 (소프트).
                                # 실측(21명 샘플): 70이면 60분 차가 남고, 200은 공백이 늘어남 — 120이 균형점.
    couple_finish_exact=600.0,  # '반드시 30분 차이' 부부(종료시간차=30) — 정확히 30분에서 벗어난 (30분 단위)^2.
                                # 같이 끝나도 위반이므로 couple_finish_gap보다 훨씬 세게 걸어 사실상 강제.
    accept_tolerance=90.0,      # 교란 후 이 정도 나빠짐까지는 받아들여 탐색을 넓힌다(점점 0으로)
)

# (26.8.14 폐지) 여성 07:30 이전 슬롯 회피 규칙 — 사용자 지시로 제거.
# 가중치 female_early_slot(W)·female_early(G)와 함께 삭제했다.

# 교류전: 같은 클럽 안에서 (최대 게임수 - 최소 게임수) 격차를 이 값 이내로 제한.
SPREAD_CAP = 2

# 여자가 이 인원 이하면 혼복을 기본으로 1~2판 넣는다.
# 여복만 돌리면 늘 같은 사람끼리 붙게 되므로, 섞어서 재미를 확보하는 용도.
MIXED_SMALL_WOMEN = 6
MIXED_MIN_GAMES = 1       # 최소 보장 판수
MIXED_DEFAULT_QUOTA = 2   # 그때의 허용 상한 (= "1~2게임 정도")

# 두 팀 구력합 차이가 이 값을 넘으면 '쓸 만한 대진'으로 치지 않는다.
# 출전 4명이 모두 구력 10년 미만이면 3, 10년 이상인 사람이 끼면 4까지 허용한다.
#   (고구력자가 끼면 합계 자체가 커서 딱 맞추기 어렵기 때문)
# 예: 방미라(3)+노남숙(3)=6  vs  서명숙(5)+정정희(5)=10 → 차이 4 → 전원 10년 미만이므로 탈락.
# 억지로 여복을 만들다 구력이 안 맞느니 다른 조합이나 혼복이 낫다는 판단.
HIGH_EXP = 10
SKILL_TOL_LOW = 3      # 전원 구력 10년 미만
SKILL_TOL_HIGH = 4     # 구력 10년 이상인 사람이 낀 대진


def skill_tol(*people) -> int:
    """이 대진에 적용할 두 팀 구력합 차이 허용치."""
    return SKILL_TOL_HIGH if any(p["exp"] >= HIGH_EXP for p in people) else SKILL_TOL_LOW


# 개인 '연속게임' 칸 → 연속으로 뛸 수 있는 최대 게임수.
# 금지=1(한 게임 뛰면 반드시 쉼) / 빈칸=2(현행 기본) / 허용=3.
# '허용'은 무제한이 아니다 — 6슬롯에 5게임 같은 보장을 위해 3연속까지만 연다.
STREAK_RUN_LIMIT = {"": 2, "no2": 1, "ok3": 3}


@lru_cache(maxsize=None)
def max_games_streak(slots: tuple, streak: str = "") -> int:
    """개인 연속게임 모드를 지키며 이 슬롯들에서 뛸 수 있는 최대 게임수.

    모드는 '연속으로 몇 게임까지 뛸 수 있는가'(run limit)로 환원된다 —
    금지(no2)=1 / 빈칸(기본)=2 / 허용(ok3)=3.
    '허용'도 무제한이 아니라 **3연속까지**다(안내 문구와 동일한 의미).
    """
    limit = STREAK_RUN_LIMIT.get(streak, STREAK_RUN_LIMIT[""])
    # best[k] = 지금까지 중 '마지막 슬롯이 k연속째'인 상태의 최대 게임수 (k=0은 그 슬롯을 안 뜀)
    best = [0] + [None] * limit
    prev = None
    for s in sorted(slots):
        adj = prev is not None and s - prev == 30
        rest = max(x for x in best if x is not None)
        nxt = [rest] + [None] * limit
        for k in range(1, limit + 1):
            if k == 1:
                src = best[0] if adj else rest   # 인접이면 직전은 쉬었어야 새 연속이 시작된다
            else:
                src = best[k - 1] if adj else None
            if src is not None:
                nxt[k] = src + 1
        best, prev = nxt, s
    return max(x for x in best if x is not None)


def max_games_no3(slots: tuple) -> int:
    """기존 호출부 호환용: 기본 3연속 금지 모드의 최대 게임수."""
    return max_games_streak(slots, "")


def eff_min_games(p: dict) -> int:
    """이 사람에게 실제로 보장해야 하는 최소 게임수.

    입력의 '최소게임수'를 '3연속 출전 금지를 지키며 가용 시간 안에 뛸 수 있는
    최대 게임수'(max_games_no3)로 자른 값. 최소게임수를 안 적었으면 0.
    (가용 슬롯 수로만 자르면 붙어 있는 슬롯에서 3연속을 강요하게 된다)
    """
    mg = p.get("min_games")
    if not mg:
        return 0
    return min(mg, max_games_streak(
        tuple(p.get("available_slots") or ()), p.get("streak") or ""))


def min_games_critical(p: dict, slot_start: int, state: dict) -> bool:
    """이 슬롯을 놓치면 최소게임수 보장이 깨지는 상태인가.

    '남은 자리'는 단순 슬롯 수가 아니라 이후 슬롯에서 3연속 금지를 지키며
    뛸 수 있는 최대 게임수로 센다 — 남은 슬롯이 붙어 있으면 다 뛸 수 없으므로
    예전 계산보다 일찍 발동해, 3연속 없이도 보장을 지킬 수 있게 미리 태운다.
    """
    deficit = eff_min_games(p) - state["player_games"][p["id"]]
    if deficit <= 0:
        return False
    later = tuple(s for s in (p.get("available_slots") or ()) if s > slot_start)
    return max_games_streak(later, p.get("streak") or "") < deficit

COURT_AFFINITY = {
    "A": {"M": 1.0, "F": 0.0, "X": 0.0},
    "B": {"M": 0.0, "F": 1.0, "X": 1.0},
    "C": {"M": 0.0, "F": 0.0, "X": 0.0},
}


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def quad_key(t1, t2) -> tuple:
    """같은 4명이 다시 만났는지 판별하는 키 (편 구성 무관)."""
    return tuple(sorted([t1[0], t1[1], t2[0], t2[1]]))


def matchup_key(t1, t2) -> tuple:
    """같은 4명이 '같은 편 구성'으로 다시 만났는지 판별하는 키.

    페어(2명)뿐 아니라 상대편까지 그대로 반복되는, 가장 재미없는 중복.
    """
    a = tuple(sorted(t1))
    b = tuple(sorted(t2))
    return (a, b) if a < b else (b, a)


def _same_club(a: dict, b: dict) -> bool:
    """두 선수가 같은 클럽 소속인지. 클럽 정보 없으면 같은 것으로 간주."""
    return a.get("club", "") == b.get("club", "")


def build_eff_in(players: list[dict]) -> dict:
    """선수별 '실질 도착 시각' = 실제로 뛸 수 있는 가장 이른 슬롯.

    IN시간이 코트 운영 전인 경우(예: 07:00 IN인데 07:00엔 코트가 없음)를 반영한다.
    대기시간을 이 기준으로 재므로 '구조상 불가능한 대기'는 페널티로 잡히지 않는다.
    (26.8.14: 여성 07:30 이전 회피 규칙 폐지 — 성별에 따른 보정 없음)
    """
    out = {}
    for p in players:
        av = p.get("available_slots") or []
        base = p["in_min"]
        cand = [s for s in av if s >= base]
        out[p["id"]] = cand[0] if cand else (av[0] if av else base)
    return out


def build_hist_penalty(players: list[dict], hist_pairs) -> dict:
    """이름쌍 히스토리([[nameA, nameB, weight], ...]) → 이번 주 id 기준 페어 패널티 맵.

    참가자 id는 매주 새로 매겨지므로 과거 페어는 이름으로만 매칭한다.
    이번 주 명단에 없는 사람이 낀 페어는 자연히 무시된다(둘 다 있어야 매핑).
    """
    name_to_id = {p["name"]: p["id"] for p in players}
    pen: dict = {}
    for entry in hist_pairs or []:
        if not entry or len(entry) < 2:
            continue
        a, b = entry[0], entry[1]
        w = float(entry[2]) if len(entry) > 2 else 1.0
        ida, idb = name_to_id.get(a), name_to_id.get(b)
        if ida and idb and ida != idb:
            k = pair_key(ida, idb)
            pen[k] = pen.get(k, 0.0) + w
    return pen


def _mixed_possible(players: list[dict], multi: bool) -> bool:
    """혼복 경기를 만들 수 있는 인원 구성인지."""
    n_m = sum(1 for p in players if p["gender"] == "M")
    n_f = len(players) - n_m
    if n_m < 2 or n_f < 2:
        return False
    if not multi:
        return True
    # 교류전은 각 팀이 '같은 클럽 남1+여1'이어야 하므로, 그런 클럽이 둘 이상 있어야 한다.
    ok_clubs = 0
    by_club: dict = {}
    for p in players:
        by_club.setdefault(p.get("club", ""), set()).add(p["gender"])
    for genders in by_club.values():
        if {"M", "F"} <= genders:
            ok_clubs += 1
    return ok_clubs >= 2


def mixed_wish_need(players: list[dict], multi: bool = False) -> int:
    """'혼복희망'을 적은 사람들을 다 태우려면 혼복이 최소 몇 판 필요한지.

    - 한 사람이 N판을 원하면 혼복은 최소 N판 있어야 한다(한 판에 한 자리).
    - 평소(단일 클럽) 혼복 1판의 자리는 남 2 · 여 2 → 같은 성별 희망 합계가 S면 ceil(S/2)판.
    - **교류전은 한 판이 '같은 클럽 남1+여1' 대 '다른 클럽 남1+여1'** 이므로
      (클럽, 성별) 조합마다 판당 자리가 **1개뿐**이다. 2로 나누면 과소계산된다
      (실측: A클럽 남자 3명이 각 1판 희망 → 2판으로 계산되지만 실제로는 3판 필요).

    아무도 안 적었으면 0 — 이 기능은 적은 사람이 있을 때만 작동한다.
    """
    seats_per_match = 1 if multi else 2
    per_group: dict = {}
    top = 0
    for p in players:
        w = p.get("mixed_wish")
        if not w:
            continue
        w = int(w)
        key = (p.get("club", ""), p["gender"]) if multi else p["gender"]
        per_group[key] = per_group.get(key, 0) + w
        top = max(top, w)
    if not top:
        return 0
    return max(top, max(-(-s // seats_per_match) for s in per_group.values()))


def mixed_limits(players: list[dict], schedule_slots: list[dict] | None,
                 multi: bool = False) -> tuple[int, int]:
    """혼복 경기 수의 (최소 보장, 허용 상한).

    - 남복/여복이 원칙이지만, **여자가 적으면 혼복을 기본으로 1~2판 넣는다.**
      (여자 6명 이하 → 여복만 돌리면 늘 같은 사람끼리라 재미가 없다)
    - 소수 성별이 4~5명이면 중복 없는 단성 복식을 몇 판밖에 못 만든다
      (여자 4명 → 편 가르는 방법이 3가지뿐 → 여복 3판이 한계).
      그 이상 뛰려면 같은 4명이 상대편까지 똑같이 다시 붙어야 하므로,
      모자란 게임수만큼 상한을 더 올린다.
    - **혼복희망(입력 '혼복희망' 칸)을 적은 사람이 있으면 그만큼 하한을 연다.**
      아무도 안 적으면 지금까지와 완전히 동일 — 이 항은 0이다.
    - 상한을 넘는 혼복은 원칙대로 급격한 페널티.
    """
    if not _mixed_possible(players, multi):
        return 0, 0

    total_matches = 0
    for sl in (schedule_slots or []):
        n_avail = sum(1 for p in players if sl["slot_start"] in p.get("available_slots", []))
        total_matches += min(len(sl["courts"]), n_avail // 4)

    wish_need = mixed_wish_need(players, multi)
    # 실현 가능한 판수를 넘는 희망은 하한이 될 수 없다. 넘겨 두면 '절대 못 채우는 하한'이
    # 남아 모든 후보에 같은 상수 페널티가 붙고, 그 사이 mixed_below_min이 대진표를 통째로
    # 혼복으로 민다. (개인 단위 클램프는 parse_input.clamp_mixed_wish가 따로 건다)
    if total_matches:
        wish_need = min(wish_need, total_matches)

    n = len(players)
    n_m = sum(1 for p in players if p["gender"] == "M")
    n_f = n - n_m
    minority = min(n_m, n_f)
    if minority < 4:
        return wish_need, 10 ** 6   # 단성 복식 자체가 불가능 → 혼복이 유일한 수단

    quota = 0
    minority_players = [p for p in players
                        if p["gender"] == ("F" if n_f <= n_m else "M")]
    # 짝도 안 겹치고 구력도 맞는 단성 복식을 몇 판까지 만들 수 있는지
    capacity = good_singles_capacity(minority_players, multi)
    if total_matches and n:
        target = round(4 * total_matches / n)          # 1인당 공평 게임수
        # 소수 성별이 필요한 총 출전 자리 − 단성 복식이 태울 수 있는 자리(1판당 4명)
        short_seats = max(0, minority * target - 4 * capacity)
        quota = -(-short_seats // 2)                   # 혼복 1판이 소수 성별 2명을 태운다

    # 하한은 **희망 판수만큼** 올린다. 한때 '혼복이 덜 나오게' 하한을 1판으로 낮췄다가
    # (26.8.14) **게임수 균형이 눈에 띄게 나빠져 되돌렸다.**
    #   원리: 하한이 희망만큼이면 그리디가 그 판수를 확실히 만들어 희망이 자연히 채워지고,
    #   40개 초안이 대부분 `mixed_wish_short`=0이 되어 **초안 선택이 게임수 균형을 보고**
    #   고를 수 있다. 하한을 낮추면 상당수 초안이 희망 미달이 되어 350×(부족)²이 선택을
    #   지배하고 균형은 뒷전으로 밀린다.
    #   실측(실전 21명, 시드 7/1/42/13, 풀타임 14명 게임수 분산):
    #     하한=희망  0.209 / 0.352 / 0.209 / 0.209  ← 기능 도입 전과 동일
    #     하한=1판   0.495 / 0.495 / 0.408 / 0.286  ← 사용자 지적("2게임 차가 늘었다")
    #   이선우 혼복 충족(2/2)은 두 경우 모두 같았다 — 낮출 이득이 없고 손해만 있었다.
    # 오타로 큰 값이 들어오는 것은 `parse_input.clamp_mixed_wish()`(경고 + 자동 클램프)와
    # 바로 위의 total_matches 상한이 막는다. 하한을 낮추는 것은 그 방어책이 아니다.
    wish_floor = wish_need
    if n_f <= MIXED_SMALL_WOMEN:
        lo = max(MIXED_MIN_GAMES, wish_floor)
        return lo, max(quota, MIXED_DEFAULT_QUOTA, wish_need)
    return wish_floor, max(quota, wish_need)


def build_couples(players: list[dict], couples) -> tuple[dict, list]:
    """이름 기준 부부 목록([[이름1, 이름2, want(, 종료시간차)], ...]) → 이번 주 id 기준 매핑.

    종료시간차(선택, 분): None=같이 끝남(30분 이내 목표) / 30=반드시 정확히 30분 차이.
    둘 다 이번 주 참가자일 때만 반영. 반환: (pair_key→want, [(ida, idb, want, gap), ...])
    """
    name_to_id = {p["name"]: p["id"] for p in players}
    pref, lst = {}, []
    for entry in couples or []:
        if not entry or len(entry) < 2:
            continue
        a, b = str(entry[0]), str(entry[1])
        want = bool(entry[2]) if len(entry) > 2 else False
        gap = None
        if len(entry) > 3 and entry[3] not in (None, "", False):
            try:
                gap = int(entry[3])
            except (TypeError, ValueError):
                gap = None
        ida, idb = name_to_id.get(a), name_to_id.get(b)
        if ida and idb and ida != idb:
            pref[pair_key(ida, idb)] = want
            lst.append((ida, idb, want, gap))
    return pref, lst


def couple_finish_cost(last_a, last_b, gap=None) -> float:
    """부부 두 사람의 마지막 경기 슬롯 차이 비용.

    gap=None(기본): 같이 끝나는 부부 — 30분 이내는 0, 그 이상은 제곱 페널티.
    gap=30: '반드시 30분 차이' 부부 — 정확히 30분에서 벗어난 만큼 제곱 페널티(같이 끝나도 위반).
    """
    if last_a is None or last_b is None:
        return 0.0
    diff = abs(last_a - last_b)
    if gap:
        u = abs(diff - gap) / 30.0
        return G["couple_finish_exact"] * u * u
    u = diff / 30.0 - 1.0
    return G["couple_finish_gap"] * u * u if u > 0 else 0.0


# ---------------------------------------------------------------------------
# 씨드(고정 배치) — 입력 양식 '씨드대진' 시트에서 사용자가 직접 정해 둔 자리.
# 알고리즘은 그 자리를 '이미 정해진 것'으로 받고 나머지만 채운다.
# ---------------------------------------------------------------------------

def pin_ids(pin: dict | None) -> frozenset:
    """이 고정 배치에 이름이 적힌 선수 id들."""
    if not pin:
        return frozenset()
    return frozenset(x for x in list(pin.get("team1") or []) + list(pin.get("team2") or []) if x)


def build_pin_index(pins, players: list[dict]) -> tuple[dict, dict, dict]:
    """pins → ((슬롯, 코트) → pin, 선수id → 고정된 슬롯 집합, (슬롯, 선수id) → 고정 코트).

    두 번째 값이 '예약'이다. 고정 자리는 뒤 슬롯에 있어도 지금 결정에 영향을 줘야 한다 —
    예약을 안 보면 앞 슬롯을 자유롭게 채운 뒤 뒤늦게 고정이 붙으면서 연속게임 한도를
    넘기거나 최대게임수를 초과한다. (입력 검증이 먼저 걸러 주지만 여기서도 명단 밖 id는 무시)

    세 번째 값은 **같은 슬롯 안에서의 예약**이다. 그리디는 한 슬롯의 코트를 순서대로 채우는데,
    뒤 코트에 고정된 사람을 앞 코트가 먼저 데려가 버리면 정작 고정 자리를 못 채운다
    (가용 인원이 빠듯한 슬롯에서 실제로 일어난다). 그래서 '이 슬롯에서 다른 코트에 고정된
    사람'은 이 코트 후보에서 아예 뺀다.
    """
    valid = {p["id"] for p in players}
    pin_map, pin_slots, pin_court_of = {}, {}, {}
    for pin in pins or []:
        ids = frozenset(i for i in pin_ids(pin) if i in valid)
        if not ids:
            continue
        court = str(pin["court"])
        pin_map[(pin["slot_start"], court)] = pin
        for pid in ids:
            pin_slots.setdefault(pid, set()).add(pin["slot_start"])
            pin_court_of[(pin["slot_start"], pid)] = court
    return pin_map, pin_slots, pin_court_of


def pins_ahead(pid: str, slot_start: int, state: dict) -> int:
    """아직 오지 않은(이 슬롯보다 뒤의) 고정 자리 수 — 최대게임수 여유에서 미리 뺀다."""
    return sum(1 for s in state.get("pin_slots", {}).get(pid, ()) if s > slot_start)


def _require_only(candidates: list, require_ids: frozenset) -> list:
    """씨드로 고정된 선수가 모두 들어간 후보만 남긴다."""
    if not require_ids:
        return candidates
    return [c for c in candidates
            if require_ids <= ({p["id"] for p in c[2]} | {p["id"] for p in c[3]})]


def _orient_for_pin(cands: list, pin: dict) -> list:
    """씨드가 지정한 '어느 팀인지'까지 맞는 후보만, 팀 방향을 맞춰서 남긴다.

    후보 생성은 (팀1, 팀2)를 한 방향으로만 만들지만 두 팀은 대칭이므로 뒤집으면 맞는
    후보도 살린다 — 안 그러면 쓸 수 있는 조합의 절반을 그냥 버린다.
    """
    need1 = frozenset(x for x in (pin.get("team1") or []) if x)
    need2 = frozenset(x for x in (pin.get("team2") or []) if x)
    out = []
    for cost, mtype, t1, t2 in cands:
        ids1 = {p["id"] for p in t1}
        ids2 = {p["id"] for p in t2}
        if need1 <= ids1 and need2 <= ids2:
            out.append((cost, mtype, t1, t2))
        elif need1 <= ids2 and need2 <= ids1:
            out.append((cost, mtype, t2, t1))
    return out


def _arrange_seats(team: tuple, want) -> tuple:
    """씨드에 적힌 칸 위치 그대로 앉힌다.

    결과 엑셀이 사용자가 적은 모양과 같아야 '내가 고정한 자리가 그대로인지'를 눈으로
    바로 확인할 수 있다. 뭔가 어긋나면(있을 수 없지만) 원래 순서를 그대로 돌려준다.
    """
    fixed = {i: pid for i, pid in enumerate(list(want or [])[:2]) if pid}
    if not fixed:
        return team
    if len(set(fixed.values())) != len(fixed):
        return team   # 같은 사람이 두 칸에 적힌 입력 — 한 사람을 두 번 앉히지 않는다
    by_id = {p["id"]: p for p in team}
    if any(pid not in by_id for pid in fixed.values()):
        return team
    seats = [None, None]
    for i, pid in fixed.items():
        seats[i] = by_id[pid]
    rest = [p for p in team if p["id"] not in set(fixed.values())]
    for i in (0, 1):
        if seats[i] is None:
            if not rest:
                return team
            seats[i] = rest.pop(0)
    return tuple(seats)


def init_state(players: list[dict], hist_pairs=None, schedule_slots=None, couples=None,
               pin_slots=None) -> dict:
    distinct_clubs = {p.get("club", "") for p in players if p.get("club", "")}
    multi = len(distinct_clubs) > 1

    # 지난주/2주전 페어 회피는 '우리 멤버끼리(단일 클럽)'일 때만 적용. 교류전(다클럽)에선 무시.
    hist_pair_penalty = {} if multi else build_hist_penalty(players, hist_pairs)

    # 게임수 균형 그룹: 교류전이면 (클럽, 성별)별로, 평소면 전체 1개 그룹.
    # 교류전에서 인원 적은 성별(예: 여자 2명)이 상대적으로 더 뛰는 건 구조상 자연스러우므로
    # 남녀를 섞어 비교하지 않고 같은 클럽·같은 성별끼리만 공평하게 맞춘다.
    def bkey(p):
        return (p.get("club", ""), p["gender"]) if multi else "__ALL__"
    bal_members = {}
    for p in players:
        bal_members.setdefault(bkey(p), []).append(p["id"])
    mixed_min, mixed_max = mixed_limits(players, schedule_slots, multi)
    couple_pref, couple_list = build_couples(players, couples)
    n_m_total = sum(1 for p in players if p["gender"] == "M")
    n_f_total = len(players) - n_m_total
    return {
        # 부부 페어: 혼복 같은 팀 회피(피함)/우대(원함) + 종료 시각 맞추기용
        "couple_pref": couple_pref,
        "couples": couple_list,
        "matches": [],
        "player_games": {p["id"]: 0 for p in players},
        "player_slots": {p["id"]: [] for p in players},
        # 사람별 혼복 출전 수 — '혼복희망' 충족 여부를 그리디가 슬롯마다 보기 위함
        "player_mixed": {p["id"]: 0 for p in players},
        "pair_count": {},
        "quad_count": {},      # 같은 4명이 다시 만난 횟수 (편 구성 무관)
        "matchup_count": {},   # 같은 4명이 같은 편 구성으로 다시 만난 횟수
        "type_count": {"M": 0, "F": 0, "X": 0},
        # 클럽이 2개 이상이면 교류전 모드 — 상대 팀은 반드시 다른 클럽(하드).
        "multi_club": multi,
        "bal_of": {p["id"]: bkey(p) for p in players},
        "bal_members": bal_members,
        "bal_count": {k: len(ids) for k, ids in bal_members.items()},
        "bal_game_sum": {k: 0 for k in bal_members},
        # 성별 그룹이 전체 평균보다 뒤처지지 않게 하는 용도 (평소=단일 클럽에서만 사용)
        "gender_count": {g: sum(1 for p in players if p["gender"] == g) for g in ("M", "F")},
        "gender_sum": {"M": 0, "F": 0},
        "hist_pair_penalty": hist_pair_penalty,
        "eff_in": build_eff_in(players),
        # 혼복 판수의 하한/상한. 교류전도 같은 규칙(팀은 같은 클럽 남1+여1이 되는지까지 확인).
        "mixed_min": mixed_min,
        "mixed_quota": mixed_max,
        # 성별별로 만들 수 있는 서로 다른 4명 조합 수. 이만큼 다 쓰고 나면
        # 그 성별의 단성 복식 재대결은 구조적으로 불가피하다(여자 4명 → 1).
        "quad_alt": {g: math.comb(n, 4) if n >= 4 else 0
                     for g, n in (("M", n_m_total), ("F", n_f_total))},
        # 씨드 예약: 이 사람이 고정으로 들어갈 슬롯들(아직 안 채워진 뒤 슬롯 포함)
        "pin_slots": {k: frozenset(v) for k, v in (pin_slots or {}).items()},
    }


def update_state(state: dict, match: dict) -> None:
    state["matches"].append(match)
    n_f = 2 if match["type"] == "X" else (4 if match["type"] == "F" else 0)
    state["gender_sum"]["F"] += n_f
    state["gender_sum"]["M"] += 4 - n_f
    for p_id in match["team1"] + match["team2"]:
        state["player_games"][p_id] += 1
        state["player_slots"][p_id].append(match["slot_start"])
        if match["type"] == "X" and p_id in state.get("player_mixed", {}):
            state["player_mixed"][p_id] += 1
        bk = state.get("bal_of", {}).get(p_id)
        if bk in state.get("bal_game_sum", {}):
            state["bal_game_sum"][bk] += 1
    for team in (match["team1"], match["team2"]):
        k = pair_key(team[0], team[1])
        state["pair_count"][k] = state["pair_count"].get(k, 0) + 1
    qk = quad_key(match["team1"], match["team2"])
    state["quad_count"][qk] = state["quad_count"].get(qk, 0) + 1
    mk = matchup_key(match["team1"], match["team2"])
    state["matchup_count"][mk] = state["matchup_count"].get(mk, 0) + 1
    state["type_count"][match["type"]] += 1


def is_two_streak(p_id: str, slot_start: int, state: dict) -> bool:
    slots = state["player_slots"][p_id]
    return (slot_start - 30) in slots


def is_three_streak(p_id: str, slot_start: int, state: dict) -> bool:
    slots = set(state["player_slots"][p_id])
    return (slot_start - 30) in slots and (slot_start - 60) in slots


def is_four_streak(p_id: str, slot_start: int, state: dict) -> bool:
    slots = set(state["player_slots"][p_id])
    return all((slot_start - d) in slots for d in (30, 60, 90))


def streak_run_len(p_id: str, slot_start: int, state: dict) -> int:
    """slot_start에 뛴다고 볼 때 그 슬롯이 속하는 연속(30분 간격) 구간의 길이.

    이미 배정된 슬롯뿐 아니라 **씨드로 예약된 슬롯**까지 함께 본다. 예약을 안 보면
    앞 슬롯을 자유롭게 채운 뒤 뒤늦게 고정 자리가 붙으면서 연속 한도를 넘긴다.
    앞뒤를 모두 훑는 이유도 그것이다 — 고정 자리는 뒤 슬롯에 있을 수 있다.
    (씨드가 없으면 그리디가 시간순으로 채우므로 뒤쪽은 항상 비어 있어
     결과는 종전의 '뒤돌아보기'와 완전히 같다)
    """
    taken = set(state["player_slots"][p_id])
    taken |= state.get("pin_slots", {}).get(p_id, frozenset())
    taken.discard(slot_start)   # 제안 슬롯 자신은 아래에서 1로 센다
    n = 1
    s = slot_start - 30
    while s in taken:
        n += 1
        s -= 30
    s = slot_start + 30
    while s in taken:
        n += 1
        s += 30
    return n


def blocks_by_streak(p: dict, slot_start: int, state: dict) -> bool:
    """이 사람을 이 슬롯에 넣으면 개인 연속 규칙(하드)을 깨는가.

    STREAK_RUN_LIMIT과 같은 기준 — 금지=1연속까지, 기본=2연속까지, 허용=3연속까지.
    ('허용'은 3연속까지라는 뜻이지 무제한이 아니다)
    """
    limit = STREAK_RUN_LIMIT.get(p.get("streak") or "", STREAK_RUN_LIMIT[""])
    return streak_run_len(p["id"], slot_start, state) > limit


def match_cost(
    team1: tuple[dict, dict],
    team2: tuple[dict, dict],
    match_type: str,
    slot_start: int,
    state: dict,
    pool_males_count: int,
    pool_females_count: int,
    court_name: str = "",
    mixed_is_fallback: bool = False,
    quad_forced: bool = False,
) -> float:
    cost = 0.0
    all_players = list(team1) + list(team2)

    t1_exp = team1[0]["exp"] + team1[1]["exp"]
    t2_exp = team2[0]["exp"] + team2[1]["exp"]
    exp_gap = abs(t1_exp - t2_exp)
    cost += W["team_skill_diff"] * exp_gap
    # 구력합 차이가 허용치를 넘으면 급증 — 억지로 짜서 재미없는 경기가 되느니 다른 조합/종류로.
    tol = skill_tol(*all_players)
    if exp_gap > tol:
        cost += W["skill_gap_over_tol"] * (exp_gap - tol) ** 2

    for team in (team1, team2):
        k = pair_key(team[0]["id"], team[1]["id"])
        prev = state["pair_count"].get(k, 0)
        if prev > 0:
            cost += W["pair_repeat"] * (prev * prev + 1)

    # 같은 4명이 또 만나는 것 — 특히 '상대편까지 그대로'인 완전 중복은 강하게 회피.
    # 다만 그 성별로 만들 새 4명 조합이 아예 없으면(quad_forced, 예: 여자 4명) 재대결은
    # 선택이 아니라 구조적 결과다. 이때까지 강하게 물면 여복이 1판에서 끝나고 나머지가
    # 전부 혼복이 되므로, 편 구성이 다른 재대결은 가볍게만 억제한다.
    # (편 구성까지 그대로면 아래 matchup_repeat이 그대로 강하게 문다)
    t1_ids = (team1[0]["id"], team1[1]["id"])
    t2_ids = (team2[0]["id"], team2[1]["id"])
    q_prev = state["quad_count"].get(quad_key(t1_ids, t2_ids), 0)
    if q_prev > 0:
        cost += W["quad_repeat_forced" if quad_forced else "quad_repeat"] * q_prev
    m_prev = state["matchup_count"].get(matchup_key(t1_ids, t2_ids), 0)
    if m_prev > 0:
        cost += W["matchup_repeat"] * (m_prev * m_prev + 1)

    # 지난주/2주전과 같은 페어 회피 (단일 클럽일 때만 채워짐). 가중치: 1주전 > 2주전.
    hist_pen = state.get("hist_pair_penalty")
    if hist_pen:
        for team in (team1, team2):
            w = hist_pen.get(pair_key(team[0]["id"], team[1]["id"]))
            if w:
                cost += W["history_pair_repeat"] * w

    eff_in = state.get("eff_in", {})
    multi_club = bool(state.get("multi_club"))
    gender_lag = {"M": 0.0, "F": 0.0}
    if not multi_club and state["player_games"]:
        avg_games = sum(state["player_games"].values()) / max(1, len(state["player_games"]))
        # 인원이 적은 성별(예: 여자 4명)은 복식 특성상 '통째로'만 늘어나 뒤처지기 쉽다.
        # 뒤처진 성별을 먼저 투입해 최종 게임수가 한쪽으로 쏠리지 않게 한다.
        for g, cnt in state["gender_count"].items():
            if cnt:
                gender_lag[g] = max(0.0, avg_games - state["gender_sum"][g] / cnt)
    else:
        avg_games = 0.0

    for p in all_players:
        mode = p.get("streak") or ""
        if is_two_streak(p["id"], slot_start, state):
            cost += W["consecutive_banned"] if mode == "no2" else W["consecutive"]
        if is_three_streak(p["id"], slot_start, state):
            if mode == "ok3" and not is_four_streak(p["id"], slot_start, state):
                cost += W["three_consec_allowed"]
            else:
                cost += W["three_consec"]

        if multi_club:
            # 교류전: 게임수 균형은 (클럽, 성별) 그룹 내부에서만 평가
            bk = state["bal_of"].get(p["id"])
            cnt = state["bal_count"].get(bk, 1)
            grp_avg = state["bal_game_sum"].get(bk, 0) / max(1, cnt)
            base_avg = grp_avg
        else:
            base_avg = avg_games
        played = state["player_games"][p["id"]]
        cost += W["game_balance"] * (played + 1 - base_avg) ** 2
        cost -= W["gender_catchup"] * gender_lag[p["gender"]]

        # 쉬고 있던 사람을 먼저 부른다 — 공백이 1시간 이상으로 벌어지기 전에 되돌린다.
        slots = state["player_slots"][p["id"]]
        ref = (max(slots) + 30) if slots else eff_in.get(p["id"], slot_start)
        pending = slot_start - ref
        if pending >= 30:
            urgency = W["idle_urgency"] * min(pending / 30.0, IDLE_URGENCY_CAP)
            # 이미 남들보다 많이 뛴 사람은 덜 당긴다 — 공백 메우기가 게임수 불균형을 만들지 않도록.
            if played > base_avg:
                urgency *= 0.35
            cost -= urgency

        # 최소게임수 미달자는 우대 투입. 이 슬롯을 놓치면 (3연속 금지를 지키면서는)
        # 보장을 더는 못 채우는 상태면 사실상 강제로 태운다.
        deficit = eff_min_games(p) - played
        if deficit > 0:
            cost -= W["min_games_deficit"] * deficit
            if min_games_critical(p, slot_start, state):
                cost -= W["min_games_critical"]

    if match_type == "F" and state.get("multi_club"):
        # 교류전: 여복(여자복식)을 최우선 — 양 클럽에 여자 2명 이상이면 혼복보다 여복.
        # 구력 균형보다 앞서도록 큰 우대(음수 비용).
        cost -= W["women_doubles_bonus"]

    if match_type == "X":
        # 최소 보장 판수를 아직 못 채웠으면 혼복을 오히려 우대해 초안에 확실히 등장시킨다.
        if state["type_count"]["X"] < state.get("mixed_min", 0):
            cost -= W["mixed_below_min"]
        if pool_males_count >= 4 or pool_females_count >= 4:
            cost += W["mixed_overuse"]
            # 평소(단일 클럽)에도 남복/여복 우선.
            # 단, 어느 한쪽 성별이 '중복 없는 단성 복식'을 더 못 만드는 상황(mixed_is_fallback)이면
            # 혼복이 정당한 차선책이므로 이 페널티를 걷는다.
            #   → "우선순위는 단성복식이지만, 페어가 상대편까지 중복될 바엔 혼복 1~2게임이 낫다"
            if (not state.get("multi_club") and not mixed_is_fallback
                    and state["type_count"]["X"] >= state.get("mixed_quota", 0)):
                cost += W["single_mixed_nonpriority"]
        # 교류전: 단성 복식(남복/여복) 우선 — 여기서도 '중복 없는 단성 복식'이 더 없으면 혼복 허용.
        if state.get("multi_club") and not mixed_is_fallback:
            cost += W["mixed_nonpriority"]
        # 남자 게스트는 혼복보다 남복 위주 — 혼복 남자 자리는 가급적 정회원이 맡는다.
        # (여자 게스트는 혼복 가능. 단 본인이 '혼복희망'을 적었고 **아직 못 채웠으면** 예외 —
        #  다 채운 뒤에는 페널티가 되살아나 원칙으로 돌아간다)
        pm = state["player_mixed"]
        def _wish_unmet(p):
            w = p.get("mixed_wish")
            return bool(w) and pm.get(p["id"], 0) < int(w)

        cost += W["guest_in_mixed"] * sum(
            1 for p in all_players
            if p["gender"] == "M" and p["membership"] == "게스트" and not _wish_unmet(p))
        # 혼복희망을 아직 못 채운 사람이 이 혼복에 있으면 우대 — 혼복 자리를 그 사람에게 준다.
        cost -= W["mixed_wish_seat"] * sum(1 for p in all_players if _wish_unmet(p))
        # 부부 페어: '피함' 부부는 같은 팀 회피, '원함' 부부는 우대
        cpref = state.get("couple_pref")
        if cpref:
            for team in (team1, team2):
                k = pair_key(team[0]["id"], team[1]["id"])
                if k in cpref:
                    cost += -W["couple_want_pair"] if cpref[k] else W["couple_avoid_pair"]
        for team in (team1, team2):
            male = team[0] if team[0]["gender"] == "M" else team[1]
            female = team[1] if team[0]["gender"] == "M" else team[0]
            if male["exp"] < female["exp"]:
                cost += W["mixed_skill_rule_violation"]

    for team in (team1, team2):
        memberships = {team[0]["membership"], team[1]["membership"]}
        if len(memberships) == 1:
            cost += W["no_member_guest_mix"]

    if court_name:
        affinity = COURT_AFFINITY.get(court_name.upper(), {}).get(match_type, 0.0)
        cost += W["court_affinity"] * affinity

    # (26.8.14 폐지) '여성 07:30 이전 슬롯 회피'는 사용자 지시로 제거 — 이제 여성도
    # 본인 IN시간부터 아무 슬롯이나 배정된다.

    return cost


def enumerate_candidates(
    pool: list[dict],
    slot_start: int,
    state: dict,
    rng: random.Random,
    top_k: int = 10,
    court_name: str = "",
    require_ids: frozenset = frozenset(),
) -> list[tuple[float, str, tuple, tuple]]:
    # 기본 모드는 3연속 하드 금지, 개인 'no2'는 2연속도 금지한다.
    # 'ok3'로 명시한 사람만 개인 면제. 최소게임수 보장은 min_games_critical이
    # 각 개인 모드를 반영해 더 일찍 발동한다.
    # 씨드로 고정된 사람은 이 필터에서 면제한다 — 사용자가 직접 지정한 자리가 우선이고,
    # 그 때문에 생기는 규칙 위반은 입력 검사에서 미리 경고한다.
    working_pool = [p for p in pool
                    if p["id"] in require_ids or not blocks_by_streak(p, slot_start, state)]
    if len(working_pool) < 4:
        return []   # 3연속 없이 코트를 채울 수 없으면 이 코트는 비운다

    eff_in = state.get("eff_in", {})

    def _pending(p):
        slots = state["player_slots"][p["id"]]
        ref = (max(slots) + 30) if slots else eff_in.get(p["id"], slot_start)
        return slot_start - ref

    # 최소게임수 미달자 → 게임수가 적은 사람 → 오래 쉰 사람 순.
    # 미달자가 top_k 밖으로 밀려 후보에조차 못 드는 일을 막는다.
    pool_sorted = sorted(
        working_pool,
        key=lambda p: (
            -max(0, eff_min_games(p) - state["player_games"][p["id"]]),
            state["player_games"][p["id"]],
            -min(_pending(p), 120),
            1 if is_two_streak(p["id"], slot_start, state) else 0,
            rng.random(),
        ),
    )
    # 씨드로 고정된 사람이 후보 슬라이스(top_k·SINGLES_TOP·MIXED_TOP) 절단에 걸리면
    # 그를 포함한 조합이 아예 생성되지 않는다 → 정렬 맨 앞으로 끌어올린다.
    if require_ids:
        pool_sorted = ([p for p in pool_sorted if p["id"] in require_ids]
                       + [p for p in pool_sorted if p["id"] not in require_ids])

    multi_club = bool(state.get("multi_club"))

    full_males = [p for p in pool if p["gender"] == "M"]
    full_females = [p for p in pool if p["gender"] == "F"]
    pool_m, pool_f = len(full_males), len(full_females)

    candidates = []

    if multi_club:
        # 교류전: 클럽별로 '게임수 적은 순' 상위 인원만 추려, cross-club 매치만 직접 생성.
        # (같은 팀=같은 클럽, 상대 팀=다른 클럽이 구조적으로 보장됨 → 낭비 열거 없음)
        per_club = max(4, (top_k + 1) // 2)
        m_by_club, f_by_club = {}, {}
        for p in pool_sorted:
            (m_by_club if p["gender"] == "M" else f_by_club).setdefault(p.get("club", ""), []).append(p)
        m_by_club = {c: lst[:per_club] for c, lst in m_by_club.items()}
        f_by_club = {c: lst[:per_club] for c, lst in f_by_club.items()}
        club_keys = sorted(set(m_by_club) | set(f_by_club))
        # 남녀 어느 한쪽이라도 '중복 없는 단성 복식(같은클럽 팀 vs 다른클럽)'을 못 만들면
        # 혼복을 차선책으로 인정한다.
        sorted_m = [p for p in pool_sorted if p["gender"] == "M"]
        sorted_f = [p for p in pool_sorted if p["gender"] == "F"]
        mixed_is_fallback = not (
            clean_singles_available(sorted_m, state)
            and clean_singles_available(sorted_f, state)
        )
        # 새 4명 조합이 없으면 재대결은 구조적으로 강제 — quad_repeat을 가볍게 (단일 클럽과 동일)
        quad_forced_m = not fresh_quad_available(sorted_m, state)
        quad_forced_f = not fresh_quad_available(sorted_f, state)
        for i in range(len(club_keys)):
            for j in range(i + 1, len(club_keys)):
                Am, Bm = m_by_club.get(club_keys[i], []), m_by_club.get(club_keys[j], [])
                Af, Bf = f_by_club.get(club_keys[i], []), f_by_club.get(club_keys[j], [])
                for pa in itertools.combinations(Am, 2):       # 남복: i클럽 vs j클럽
                    for pb in itertools.combinations(Bm, 2):
                        candidates.append((match_cost(pa, pb, "M", slot_start, state, pool_m, pool_f, court_name,
                                                      quad_forced=quad_forced_m), "M", pa, pb))
                for pa in itertools.combinations(Af, 2):       # 여복
                    for pb in itertools.combinations(Bf, 2):
                        candidates.append((match_cost(pa, pb, "F", slot_start, state, pool_m, pool_f, court_name,
                                                      quad_forced=quad_forced_f), "F", pa, pb))
                for am in Am:                                  # 혼복: (남1+여1) vs (남1+여1)
                    for af in Af:
                        t1 = (am, af)
                        for bm in Bm:
                            for bf in Bf:
                                t2 = (bm, bf)
                                candidates.append((match_cost(t1, t2, "X", slot_start, state, pool_m, pool_f,
                                                              court_name, mixed_is_fallback), "X", t1, t2))
        return _require_only(candidates, require_ids)

    # 단일 클럽(평소): 기존 로직
    top = pool_sorted[: min(len(pool_sorted), top_k)]
    males = [p for p in top if p["gender"] == "M"]
    females = [p for p in top if p["gender"] == "F"]

    # 여복은 여자 인원이 적어 top_k 안에 4명이 다 들어오지 못할 수 있다.
    # 남복/여복 우선 원칙을 지키려면 후보 자체가 만들어져야 하므로,
    # 풀 전체에 여자가 4명 이상이면 게임수 적은 순 4명을 따로 확보한다.
    if len(females) < 4 and pool_f >= 4:
        females = [p for p in pool_sorted if p["gender"] == "F"][:max(4, len(females))]
    if len(males) < 4 and pool_m >= 4:
        males = [p for p in pool_sorted if p["gender"] == "M"][:max(4, len(males))]

    # 조합 폭발 방지 — 어차피 '게임수 적은 순'으로 정렬돼 있어 뒤쪽은 거의 선택되지 않는다.
    singles_m = males[:SINGLES_TOP]
    singles_f = females[:SINGLES_TOP]
    mixed_m = males[:MIXED_TOP]
    mixed_f = females[:MIXED_TOP]

    # 이 성별로 '아직 안 붙어본 4명 조합'이 남아있는가 — 없으면 재대결은 구조적으로 강제된다
    # (예: 여자 4명 → 조합이 하나뿐). 그때는 quad_repeat을 가볍게 물어 여복이 끊기지 않게 한다.
    quad_forced_m = not fresh_quad_available(singles_m, state)
    quad_forced_f = not fresh_quad_available(singles_f, state)

    if len(singles_m) >= 4:
        for combo in itertools.combinations(singles_m, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "M", slot_start, state, pool_m, pool_f, court_name,
                               quad_forced=quad_forced_m)
                candidates.append((c, "M", t1, t2))

    if len(singles_f) >= 4:
        for combo in itertools.combinations(singles_f, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "F", slot_start, state, pool_m, pool_f, court_name,
                               quad_forced=quad_forced_f)
                candidates.append((c, "F", t1, t2))

    if len(mixed_m) >= 2 and len(mixed_f) >= 2:
        # 남녀 어느 한쪽이라도 '중복 없는 단성 복식'을 더 못 만들면 혼복은 차선책으로 인정.
        mixed_is_fallback = not (
            clean_singles_available(males, state) and clean_singles_available(females, state)
        )
        for m_combo in itertools.combinations(mixed_m, 2):
            for f_combo in itertools.combinations(mixed_f, 2):
                for swap in (False, True):
                    if not swap:
                        t1 = (m_combo[0], f_combo[0])
                        t2 = (m_combo[1], f_combo[1])
                    else:
                        t1 = (m_combo[0], f_combo[1])
                        t2 = (m_combo[1], f_combo[0])
                    c = match_cost(t1, t2, "X", slot_start, state, pool_m, pool_f, court_name,
                                   mixed_is_fallback)
                    candidates.append((c, "X", t1, t2))

    return _require_only(candidates, require_ids)


SPLITS_OF_4 = (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)))


def clean_singles_available(same_gender_pool: list[dict], state: dict, limit: int = 8) -> bool:
    """이 성별만으로 '쓸 만한' 단성 복식을 아직 만들 수 있는가.

    쓸 만하다 = ① 같은 짝이 처음이고 ② 같은 편 구성으로 다시 붙는 것도 아니고
              ③ 두 팀 구력합 차이가 허용치 이내(전원 10년 미만이면 3, 10년 이상 포함이면 4).

    같은 4명이 다시 만나는 것(quad)은 **새 4명 조합을 만들 수 있는 동안에만** 배제한다.
    여자가 4명뿐이면 조합이 하나뿐이라 여기서 잘라버리면 여복이 1판에서 끝나는데,
    편을 바꾸면 3판까지 짝도 상대 구성도 겹치지 않는다
    (편 가르기 3가지가 6개 짝을 한 번씩 나눠 쓴다). 그동안은 여복이 여전히 '쓸 만한' 선택지다.

    False면 지금 단성 복식을 짜봐야 짝이 겹치거나, 같은 대진이 또 나오거나,
    구력이 안 맞는 재미없는 경기가 된다는 뜻 → 혼복이 정당한 차선책이 된다.
    """
    people = same_gender_pool[:limit]
    if len(people) < 4:
        return False
    pc, mc, qc = state["pair_count"], state["matchup_count"], state["quad_count"]
    multi = bool(state.get("multi_club"))
    forced_ok = False       # 새 4명 조합은 없지만, 편을 바꾸면 아직 쓸 만한 경우
    for combo in itertools.combinations(people, 4):
        fresh_quad = not qc.get(
            quad_key((combo[0]["id"], combo[1]["id"]), (combo[2]["id"], combo[3]["id"])))
        if forced_ok and not fresh_quad:
            continue                                # 이미 같은 결론 — 더 볼 것 없다
        for (i, j), (k, l) in SPLITS_OF_4:
            p1, p2, p3, p4 = combo[i], combo[j], combo[k], combo[l]
            if multi:
                # 교류전: 같은 팀은 같은 클럽, 상대는 다른 클럽이어야 성립하는 조합만 본다.
                if not _same_club(p1, p2) or not _same_club(p3, p4) or _same_club(p1, p3):
                    continue
            if abs((p1["exp"] + p2["exp"]) - (p3["exp"] + p4["exp"])) > skill_tol(p1, p2, p3, p4):
                continue                            # 구력이 안 맞는 대진은 '쓸 만하다'로 안 본다
            t1 = (p1["id"], p2["id"])
            t2 = (p3["id"], p4["id"])
            if pc.get(pair_key(*t1)) or pc.get(pair_key(*t2)):
                continue
            if mc.get(matchup_key(t1, t2)):
                continue
            if fresh_quad:
                return True                         # 처음 붙는 4명 — 최선
            forced_ok = True                        # 재대결이지만 짝·편 구성은 새롭다
            break
    return forced_ok


def fresh_quad_available(same_gender_pool: list[dict], state: dict, limit: int = 8) -> bool:
    """이 성별 풀에 '아직 한 번도 안 붙어본 4명 조합'이 남아있는가.

    False면 이 성별의 단성 복식은 무조건 재대결이 된다(예: 여자 4명 → 조합 1개).
    그때의 재대결은 선택이 아니라 구조적 결과이므로 quad_repeat을 가볍게 문다.
    """
    people = same_gender_pool[:limit]
    if len(people) < 4:
        return False
    qc = state["quad_count"]
    for combo in itertools.combinations(people, 4):
        if not qc.get(quad_key((combo[0]["id"], combo[1]["id"]),
                               (combo[2]["id"], combo[3]["id"]))):
            return True
    return False


def good_singles_capacity(same_gender_players: list[dict], multi: bool = False) -> int:
    """짝도 안 겹치고 구력도 맞는 단성 복식을 최대 몇 판까지 만들 수 있는가.

    혼복 허용량을 정하는 근거. 예를 들어 여자가 4명이고 구력이 3/5/7/3이면
    편 가르는 방법 3가지 중 (3+3 vs 5+7)은 구력합 6 차이라 쓸 수 없어 2판이 한계다.
    → 나머지 게임수는 혼복으로 채워야 한다.
    """
    n = len(same_gender_players)
    if n < 4:
        return 0
    count = 0
    usable_pairs = set()
    for combo in itertools.combinations(same_gender_players, 4):
        for (i, j), (k, l) in SPLITS_OF_4:
            p1, p2, p3, p4 = combo[i], combo[j], combo[k], combo[l]
            if multi and (not _same_club(p1, p2) or not _same_club(p3, p4) or _same_club(p1, p3)):
                continue
            if abs((p1["exp"] + p2["exp"]) - (p3["exp"] + p4["exp"])) <= skill_tol(p1, p2, p3, p4):
                count += 1
                usable_pairs.add(pair_key(p1["id"], p2["id"]))
                usable_pairs.add(pair_key(p3["id"], p4["id"]))
    # 짝이 겹치지 않으려면 한 판에 페어 2개를 쓴다.
    # 이때 쓸 수 있는 페어는 '구력 조건을 통과한 대진에 실제로 등장하는 페어'뿐이다.
    # (예: 여자 3/3/5/5/7에서 3+3 짝은 어떤 편 구성으로도 구력차 3을 못 맞춰 아예 못 쓴다)
    return min(count, len(usable_pairs) // 2)


def _hard_filter(
    cands: list[tuple],
    slot_start: int,
    state: dict,
    exempt: frozenset = frozenset(),
) -> list[tuple]:
    """개인 연속게임 하드 규칙을 깨는 후보를 무조건 제거.

    (enumerate_candidates가 안전 풀만 쓰므로 평소엔 통과만 하는 이중 안전장치)
    exempt = 씨드로 고정된 선수 — 사용자 지정이 우선이므로 여기서도 면제한다.
    """
    return [
        entry for entry in cands
        if not any(blocks_by_streak(p, slot_start, state)
                   for p in list(entry[2]) + list(entry[3])
                   if p["id"] not in exempt)
    ]


def pick_match(
    pool: list[dict],
    slot_start: int,
    court: str,
    state: dict,
    rng: random.Random,
    candidate_top_n: int = 24,
    pin: dict | None = None,
) -> dict | None:
    req = pin_ids(pin)
    cands = enumerate_candidates(pool, slot_start, state, rng, court_name=court, require_ids=req)
    if not cands:
        return None
    cands = _hard_filter(cands, slot_start, state, exempt=req)
    if not cands:
        return None   # 3연속을 만들지 않고는 채울 수 없는 코트 → 공석
    if pin:
        cands = _orient_for_pin(cands, pin)
        if not cands:
            return None   # 씨드가 지정한 팀 배치를 만족하는 조합이 없음 → 공석 (review가 보고)

    cands.sort(key=lambda x: x[0])
    pick_pool = cands[: min(len(cands), candidate_top_n)]
    weights = [1.0 / (1.0 + i) ** 2 for i in range(len(pick_pool))]
    chosen = rng.choices(pick_pool, weights=weights, k=1)[0]
    _, mtype, t1, t2 = chosen
    if pin:
        t1 = _arrange_seats(t1, pin.get("team1"))
        t2 = _arrange_seats(t2, pin.get("team2"))
    m = {
        "slot_start": slot_start,
        "slot_end": slot_start + 30,
        "court": court,
        "type": mtype,
        "team1": [t1[0]["id"], t1[1]["id"]],
        "team2": [t2[0]["id"], t2[1]["id"]],
        "team1_names": [t1[0]["name"], t1[1]["name"]],
        "team2_names": [t2[0]["name"], t2[1]["name"]],
        "team1_exp_sum": t1[0]["exp"] + t1[1]["exp"],
        "team2_exp_sum": t2[0]["exp"] + t2[1]["exp"],
    }
    # 씨드가 없는 주에는 이 키를 아예 붙이지 않는다 — 결과 JSON이 종전과 바이트 단위로
    # 같아야 '기능 미사용 시 무변화'를 회귀로 확인할 수 있다.
    if req:
        m["pinned"] = sorted(req)
    return m


# ---------------------------------------------------------------------------
# 전체 대진표 평가 (시드 선택 + 로컬 개선 공통 목적함수)
# ---------------------------------------------------------------------------

def player_timing_cost(slots_sorted: list[int], eff_in: int, streak: str = "") -> float:
    """한 사람의 '시간표 품질' 비용.

    - 도착 후 첫 경기까지의 대기 (일찍 온 사람이 일찍 끝나도록)
    - 경기 사이 공백, 특히 1시간 이상 공백 (최우선 회피)
    - 2슬롯/3슬롯 연속 출전
    """
    if not slots_sorted:
        return 0.0
    cost = 0.0
    idle = 0

    delay = slots_sorted[0] - eff_in
    if delay > 0:
        idle += delay
        u = delay / 30.0
        cost += G["start_delay"] * u
        if u > 2:
            cost += G["long_start_delay"] * (u - 2) ** 2

    for i in range(1, len(slots_sorted)):
        gap = slots_sorted[i] - slots_sorted[i - 1] - 30
        if gap > 0:
            idle += gap
            u = gap / 30.0
            cost += G["gap"] * u
            if u >= 2:
                cost += G["long_gap"] * (u - 1) ** 2

    # 한 사람에게 노는 시간이 몰리지 않도록 볼록(제곱) 페널티.
    # 총 유휴시간은 코트 사정상 거의 정해져 있으므로, 이 항이 그 부담을 고르게 나눈다.
    # 결과적으로 일찍 온 사람이 일찍 끝나고(체류 = 유휴 + 경기시간), 늦게 온 사람이 뒤를 맡는다.
    free_units = max(0.0, idle / 30.0 - 1.0)
    if free_units > 0:
        cost += G["idle_sq"] * free_units ** 2

    # 연속 구간 길이(run)로 판정한다. 기본 모드(빈칸)의 비용은 종전과 완전히 동일하고,
    # '허용'만 3연속을 소프트로 풀되 4연속부터는 기본과 같은 하드 비용을 문다.
    run = 0
    for i in range(len(slots_sorted)):
        run = run + 1 if (i >= 1 and slots_sorted[i - 1] == slots_sorted[i] - 30) else 1
        if run >= 4:
            cost += G["three_streak"]
        elif run == 3:
            cost += G["three_streak_allowed"] if streak == "ok3" else G["three_streak"]
        elif run == 2:
            cost += G["two_streak_banned"] if streak == "no2" else G["two_streak"]

    return cost


def match_quality_cost(match: dict, players_by_id: dict, multi_club: bool,
                       couple_pref: dict | None = None) -> float:
    """한 경기의 품질 비용 (구력차 / 혼복 규칙 / 코트 / 이른 슬롯 여성 / 정회원·게스트 / 부부 페어)."""
    t1 = [players_by_id[i] for i in match["team1"]]
    t2 = [players_by_id[i] for i in match["team2"]]
    exp_gap = abs((t1[0]["exp"] + t1[1]["exp"]) - (t2[0]["exp"] + t2[1]["exp"]))
    cost = G["team_skill_diff"] * exp_gap
    tol = skill_tol(*t1, *t2)
    if exp_gap > tol:
        cost += G["skill_gap_over_tol"] * (exp_gap - tol) ** 2

    mtype = match["type"]
    if mtype == "X":
        for team in (t1, t2):
            male = team[0] if team[0]["gender"] == "M" else team[1]
            female = team[1] if team[0]["gender"] == "M" else team[0]
            if male["exp"] < female["exp"]:
                cost += G["mixed_skill_violation"]
        # 남자 게스트는 혼복보다 남복 위주 — 로컬 개선(선수 교환)이 혼복에서 빼내게 한다.
        # (여자 게스트는 혼복 가능)
        #
        # 혼복희망자도 여기서는 면제하지 않는다. 면제하면 '희망을 채운 뒤'에도 혼복 자리가
        # 공짜가 되어 과충족을 막을 힘이 사라진다(26.8.14 실측: 희망 1인데 2판, 남자 게스트
        # 혼복 자리 1→2). 희망은 Refiner의 `_wish_delta`(mixed_wish_short, 충족되면 0)가
        # 끌어오고, 그 힘(350×부족²)이 guest_in_mixed(25)보다 훨씬 크므로 미충족일 때는
        # 확실히 이기고 충족된 뒤에는 이 페널티가 되살아나 원칙(남복 위주)으로 돌아간다.
        cost += G["guest_in_mixed"] * sum(
            1 for p in t1 + t2 if p["gender"] == "M" and p["membership"] == "게스트")
        # 부부 페어: '피함' 부부 같은 팀 페널티 / '원함' 부부 같은 팀 보너스
        if couple_pref:
            for team_ids in (match["team1"], match["team2"]):
                k = pair_key(team_ids[0], team_ids[1])
                if k in couple_pref:
                    cost += -G["couple_want_pair"] if couple_pref[k] else G["couple_avoid_pair"]
        # 혼복 자체의 개수 페널티는 허용량(mixed_quota)과 함께 봐야 하므로 full_score에서 계산한다.
    elif mtype == "F" and multi_club:
        cost -= G["women_doubles_bonus"]

    cost += G["court_affinity"] * COURT_AFFINITY.get(str(match["court"]).upper(), {}).get(mtype, 0.0)

    # (26.8.14 폐지) '여성 07:30 이전 슬롯 회피' 제거 — 사용자 지시.

    for team in (t1, t2):
        if team[0]["membership"] == team[1]["membership"]:
            cost += G["member_guest"]

    return cost


def _quad_cost(count: int, forced: bool = False) -> float:
    if count <= 1:
        return 0.0
    return G["quad_repeat_forced" if forced else "quad_repeat"] * (count - 1)


def _quad_gender(key, players_by_id: dict):
    """이 4명이 모두 같은 성별이면 그 성별('M'/'F'), 혼복이면 None."""
    genders = {players_by_id[i]["gender"] for i in key if i in players_by_id}
    return genders.pop() if len(genders) == 1 else None


def quad_forced_gender(state: dict, players_by_id: dict) -> dict:
    """성별별로 '만들 수 있는 4명 조합을 이미 다 써서 재대결이 불가피한' 상태인가.

    여자 4명이면 조합이 1개뿐이라 첫 여복 직후 True가 된다 → 두 번째 여복부터는
    quad_repeat을 가볍게 물어(여복이 1판에서 끊겨 나머지가 전부 혼복이 되는 것을 막는다).
    """
    quad_alt = state.get("quad_alt") or {}
    used = {"M": 0, "F": 0}
    for k in state["quad_count"]:
        g = _quad_gender(k, players_by_id)
        if g:
            used[g] += 1
    return {g: used[g] >= quad_alt.get(g, 10 ** 6) for g in ("M", "F")}


def _matchup_cost(count: int) -> float:
    return G["matchup_repeat"] * (count - 1) ** 2 if count > 1 else 0.0


def pair_entry_cost(count: int, hist_w: float) -> float:
    cost = 0.0
    if count > 1:
        # 그리디와 같은 초선형 형태. 같은 짝이 3~4번 반복되면 비용이 급증해,
        # (여자 인원이 적어 여복 페어가 고정되는 교류전 등에서) 혼복이 차선책으로 열린다.
        cost += G["pair_dup"] * (count - 1) ** 2
    if hist_w:
        cost += G["history_pair"] * hist_w * count
    return cost


def fair_targets(ids: list[str], caps: dict, total_games: int) -> dict:
    """'공평한 배정 게임수'를 물채우기(water-filling)로 계산.

    최대게임수 제한이나 짧은 참석시간 때문에 애초에 많이 못 뛰는 사람이
    평균을 끌어내려 다른 사람까지 저평가되는 문제를 없앤다.
    """
    pool = list(ids)
    remaining = float(total_games)
    out = {}
    while pool:
        share = remaining / len(pool) if remaining > 0 else 0.0
        capped = [i for i in pool if caps.get(i, 10 ** 6) < share]
        if not capped:
            for i in pool:
                out[i] = share
            break
        for i in capped:
            out[i] = float(caps.get(i, 0))
            remaining -= out[i]
            pool.remove(i)
    return out


def balance_cost(state: dict, players: list[dict]) -> float:
    pbid = {p["id"]: p for p in players}
    caps = {
        p["id"]: min(
            p["max_games"] if p.get("max_games") else 10 ** 6,
            len(p.get("available_slots") or []),
        )
        for p in players
    }
    groups = (state["bal_members"].values() if state.get("multi_club")
              else [[p["id"] for p in players]])
    cost = 0.0
    for ids in groups:
        if not ids:
            continue
        total = sum(state["player_games"][i] for i in ids)
        targets = fair_targets(list(ids), caps, total)
        devs = [state["player_games"][i] - targets[i] for i in ids]
        cost += G["balance_sq"] * sum(d * d for d in devs)
        cost += G["balance_spread"] * (max(devs) - min(devs))
        # 공평 기준(내림)보다도 덜 뛴 사람은 사실상 금지 — 쉬는 시간·공백보다 우선한다.
        for i in ids:
            g = state["player_games"][i]
            short = int(targets[i] + 1e-9) - g
            if short > 0:
                cost += G["balance_under"] * short * short
            # 반올림 여유(0.5게임)를 넘겨 못 미치면 별도로 가산.
            frac = targets[i] - g - 0.5
            if frac > 0:
                cost += G["balance_short"] * frac * frac

        # 성별 쏠림 방지: 인원이 적은 성별(예: 여자 4명)은 복식 특성상 '통째로' 움직여
        # 부족분 몫을 매번 그 그룹이 지게 되기 쉽다. 그룹 평균으로 한 번 더 본다.
        games = [state["player_games"][i] for i in ids]
        overall = sum(games) / len(ids)
        by_gender = {}
        for i in ids:
            by_gender.setdefault(pbid[i]["gender"], []).append(state["player_games"][i])
        for gs in by_gender.values():
            lag = overall - (sum(gs) / len(gs)) - 0.25
            if lag > 0:
                cost += G["gender_fairness"] * lag * lag
    return cost


def full_score(state: dict, players: list[dict], schedule_slots: list[dict]) -> float:
    players_by_id = {p["id"]: p for p in players}
    multi = bool(state.get("multi_club"))
    hist = state.get("hist_pair_penalty") or {}
    score = balance_cost(state, players)

    # 사람별 시간표 품질
    eff_in = state.get("eff_in", {})
    for p in players:
        pid = p["id"]
        score += player_timing_cost(
            sorted(state["player_slots"][pid]), eff_in.get(pid, p["in_min"]), p.get("streak") or "")

    # 페어 중복 + 지난 대진표 반복
    for k, c in state["pair_count"].items():
        score += pair_entry_cost(c, hist.get(k, 0.0))

    # 같은 4명 재대결 (편 구성 무관 / 상대편까지 그대로)
    # 단성 복식의 재대결은 '그 성별의 4명 조합을 이미 다 써버린' 경우에만 불가피하다.
    # (여자 4명 → 조합 1개 → 두 번째 여복부터 무조건 재대결. 이때 강하게 물면 여복이 1판에서 끝난다)
    forced_g = quad_forced_gender(state, players_by_id)
    for k, c in state["quad_count"].items():
        g = _quad_gender(k, players_by_id)
        score += _quad_cost(c, bool(g) and forced_g[g])
    for c in state["matchup_count"].values():
        score += _matchup_cost(c)

    # 경기별 품질
    cpref = state.get("couple_pref")
    for m in state["matches"]:
        score += match_quality_cost(m, players_by_id, multi, cpref)

    # 혼복 개수: 허용량(단성 복식만으로는 소수 성별의 게임수를 못 채우는 만큼)까지는 가볍게,
    # 그 이상은 급격히 비싸게 — "우선순위는 남복/여복, 혼복은 1~2판까지"를 그대로 표현.
    n_mixed = state["type_count"]["X"]
    quota = state.get("mixed_quota", 0)
    score += G["mixed_match"] * min(n_mixed, quota)
    score += G["mixed_over_quota"] * max(0, n_mixed - quota) ** 2
    # 여자가 적으면(6명 이하) 혼복이 0판인 대진표는 피한다 — 여복만 돌면 늘 같은 사람끼리다.
    score += G["mixed_below_min"] * max(0, state.get("mixed_min", 0) - n_mixed) ** 2

    # 개인별 최소게임수 보장 — 어기지 않는 규칙. 미달인 초안은 사실상 선택되지 않는다.
    for p in players:
        short = eff_min_games(p) - state["player_games"][p["id"]]
        if short > 0:
            score += G["min_games_short"] * short * short

    # 개인별 혼복희망 — 적은 사람이 있을 때만 작동(아무도 안 적으면 이 루프는 비용 0).
    # 매치에서 직접 세므로 Refiner가 선수를 바꿔도 항상 실제 값과 일치한다.
    if any(p.get("mixed_wish") for p in players):
        mixed_played = {}
        for m in state["matches"]:
            if m["type"] != "X":
                continue
            for pid in m["team1"] + m["team2"]:
                mixed_played[pid] = mixed_played.get(pid, 0) + 1
        for p in players:
            w = p.get("mixed_wish")
            if not w:
                continue
            short = int(w) - mixed_played.get(p["id"], 0)
            if short > 0:
                score += G["mixed_wish_short"] * short * short

    # 부부는 같이 오간다 — 마지막 경기 종료가 같거나 30분 안쪽 차이가 되게 (소프트)
    # 단, 종료시간차=30 부부(신혁재·방미라)는 반대로 '정확히 30분 차이'를 목표로 한다.
    for ida, idb, _w, gap in state.get("couples", []):
        sa = state["player_slots"].get(ida) or []
        sb = state["player_slots"].get(idb) or []
        score += couple_finish_cost(max(sa) if sa else None, max(sb) if sb else None, gap)

    # 비어버린 코트-슬롯
    feasible_court_slots = 0
    for sl in schedule_slots:
        n_courts = len(sl["courts"])
        n_avail = sum(1 for p in players if sl["slot_start"] in p["available_slots"])
        feasible_court_slots += min(n_courts, n_avail // 4)
    score += max(0, feasible_court_slots - len(state["matches"])) * G["missed"]

    # 씨드(고정 배치) 미반영 자리 — 씨드가 없으면 pins가 비어 0이다(기존 동작 유지).
    pins = state.get("pins") or []
    if pins:
        at = {(m["slot_start"], str(m["court"])): m for m in state["matches"]}
        missing = 0
        for pin in pins:
            m = at.get((pin["slot_start"], str(pin["court"])))
            for side in SIDES:
                for pid in (pin.get(side) or []):
                    if pid and (m is None or pid not in m[side]):
                        missing += 1
        score += G["seed_missing"] * missing

    return score


# ---------------------------------------------------------------------------
# 로컬 개선 (Iterated Local Search)
#   두 가지 수술로 목적함수를 낮춘다. 둘 다 경기 수·경기 종류 총계·개인 게임수를 보존한다.
#   (1) 선수 교환: 같은 성별 두 선수를 맞바꾼다(다른 슬롯이면 시간표가, 같은 슬롯이면 페어가 바뀐다).
#   (2) 경기 이동: 두 경기의 출전 명단을 통째로 맞바꾼다 = 경기 시간대 교체.
#       여복처럼 '멤버가 고정된 경기'는 (1)로는 절대 못 옮기므로 (2)가 필요하다.
#   개선이 멈추면 무작위 교란(kick) 후 다시 내리막을 타고, 가장 좋았던 상태를 보관한다.
#   → 그리디가 빠지는 국소최적(예: 공백이 60분이 된 뒤에야 여복이 배정되는 현상)을 탈출한다.
# ---------------------------------------------------------------------------
SIDES = ("team1", "team2")
ROSTER_FIELDS = ("type", "team1", "team2", "team1_names", "team2_names",
                 "team1_exp_sum", "team2_exp_sum")


class Refiner:
    def __init__(self, state: dict, players: list[dict], schedule_slots: list[dict], rng: random.Random):
        self.state = state
        self.players = players
        self.schedule_slots = schedule_slots
        self.rng = rng
        self.pbid = {p["id"]: p for p in players}
        self.multi = bool(state.get("multi_club"))
        self.hist = state.get("hist_pair_penalty") or {}
        self.eff_in = state.get("eff_in", {})
        self.avail = {p["id"]: set(p.get("available_slots") or []) for p in players}
        # 부부: 혼복 페어 항은 quality()가, 종료시각 맞추기는 교환 delta가 직접 본다
        self.cpref = state.get("couple_pref") or {}
        self.partner = {}
        self.couple_gap = {}   # pair_key → 종료시간차 목표(None=이내 / 30=정확히 30분)
        for ida, idb, _w, gap in state.get("couples", []):
            self.partner[ida] = idb
            self.partner[idb] = ida
            self.couple_gap[pair_key(ida, idb)] = gap
        self._sync()

    def _finish_delta(self, new_slots_by_pid: dict) -> float:
        """일부 선수의 시간표가 new_slots_by_pid로 바뀔 때 부부 종료시각 비용의 변화량."""
        if not self.partner:
            return 0.0
        delta, seen = 0.0, set()
        for pid in new_slots_by_pid:
            mate = self.partner.get(pid)
            if not mate:
                continue
            k = pair_key(pid, mate)
            if k in seen:
                continue
            seen.add(k)
            old_a = self.slots_of[pid][-1] if self.slots_of[pid] else None
            new_sl = new_slots_by_pid[pid]
            new_a = new_sl[-1] if new_sl else None
            mate_sl_new = new_slots_by_pid.get(mate, self.slots_of[mate])
            old_b = self.slots_of[mate][-1] if self.slots_of[mate] else None
            new_b = mate_sl_new[-1] if mate_sl_new else None
            gap = self.couple_gap.get(k)
            delta += couple_finish_cost(new_a, new_b, gap) - couple_finish_cost(old_a, old_b, gap)
        return delta

    def _wish_delta(self, changes: dict) -> float:
        """일부 선수의 혼복 출전 수가 changes만큼 바뀔 때 '혼복희망 미달' 비용의 변화량.

        full_score의 `mixed_wish_short`(350×부족²)와 같은 식이다. Refiner는 교환을
        **델타로만** 판정하므로(전역 점수는 재계산 때만 본다) 이 항이 없으면 혼복희망을
        아예 못 본다. 충족된 뒤에는 부족=0이라 델타도 0 → 과충족을 부추기지 않는다.
        (`_finish_delta`가 부부 종료시각에 대해 하는 일과 같은 패턴)
        """
        if not self.wish:
            return 0.0
        d = 0.0
        for pid, ch in changes.items():
            w = self.wish.get(pid)
            if not w or not ch:
                continue
            cur = self.mixed_of.get(pid, 0)
            old_s = max(0, w - cur)
            new_s = max(0, w - (cur + ch))
            d += G["mixed_wish_short"] * (new_s * new_s - old_s * old_s)
        return d

    @staticmethod
    def _mixed_change(m1, m2, a_id, b_id) -> dict:
        """선수 교환으로 혼복 출전 수가 어떻게 바뀌는지. 둘 다 혼복이거나 둘 다 아니면 변화 없음."""
        x1 = m1["type"] == "X"
        x2 = m2["type"] == "X"
        if x1 == x2:
            return {}
        return {a_id: (1 if x2 else -1), b_id: (1 if x1 else -1)}

    # -- 파생 캐시 -----------------------------------------------------------
    def _sync(self) -> None:
        self.matches = self.state["matches"]
        self.slots_of = {pid: sorted(sl) for pid, sl in self.state["player_slots"].items()}
        # 씨드로 고정된 좌석은 교환 후보에서 아예 뺀다. 같은 경기의 나머지 자리는 그대로
        # 최적화 대상이므로 '경기 통째 잠금'보다 자유도가 크다.
        # (증분 delta만 보는 Refiner에 잠금을 안 걸면 로컬 개선이 조용히 씨드를 갈아엎는다 —
        #  과거 _wish_delta·timing(streak)와 같은 자리다)
        self.positions = [
            (mi, side, idx)
            for mi in range(len(self.matches))
            for side in SIDES
            for idx in (0, 1)
            if not self._is_pinned(mi, side, idx)
        ]
        # 교환 후보는 '같은 성별(교류전이면 같은 클럽)'끼리만 가능하므로 미리 묶어 둔다.
        self.pos_groups = {}
        for pos in self.positions:
            mi, side, idx = pos
            self.pos_groups.setdefault(self._group_key(self.matches[mi][side][idx]), []).append(pos)
        # 혼복희망: 희망자와 현재 혼복 출전 수. 매치에서 직접 세므로 교환 후에도 정확하다.
        self.wish = {p["id"]: int(p["mixed_wish"]) for p in self.players if p.get("mixed_wish")}
        self.mixed_of = {}
        for m in self.matches:
            if m["type"] != "X":
                continue
            for pid in m["team1"] + m["team2"]:
                self.mixed_of[pid] = self.mixed_of.get(pid, 0) + 1
        # 그리디가 세던 state["player_mixed"]는 교환으로 어긋나므로 여기서 실제 값으로 맞춘다.
        pm = self.state.get("player_mixed")
        if pm is not None:
            for pid in pm:
                pm[pid] = self.mixed_of.get(pid, 0)
        self.qcache = [self.quality(m) for m in self.matches]
        self.tcache = {pid: self.timing(pid, sl) for pid, sl in self.slots_of.items()}
        # 성별별 '4명 조합 소진' 여부 — 재대결 비용을 full_score와 같은 기준으로 매기기 위함
        self.quad_forced = quad_forced_gender(self.state, self.pbid)

    def _is_pinned(self, mi, side, idx) -> bool:
        """이 좌석이 씨드로 고정된 자리인가."""
        pinned = self.matches[mi].get("pinned")
        return bool(pinned) and self.matches[mi][side][idx] in pinned

    def _group_key(self, pid):
        p = self.pbid[pid]
        return (p["gender"], p.get("club", "")) if self.multi else p["gender"]

    def timing(self, pid, slots) -> float:
        return player_timing_cost(
            slots, self.eff_in.get(pid, self.pbid[pid]["in_min"]),
            self.pbid[pid].get("streak") or "")

    def quality(self, m) -> float:
        return match_quality_cost(m, self.pbid, self.multi, self.cpref)

    def score(self) -> float:
        return full_score(self.state, self.players, self.schedule_slots)

    def snapshot(self):
        return (
            [{k: (list(v) if isinstance(v, list) else v) for k, v in m.items()} for m in self.matches],
            {k: list(v) for k, v in self.state["player_slots"].items()},
            dict(self.state["pair_count"]),
            dict(self.state["quad_count"]),
            dict(self.state["matchup_count"]),
        )

    def restore(self, snap) -> None:
        ms, ps, pc, qc, mc = snap
        self.state["matches"] = [{k: (list(v) if isinstance(v, list) else v) for k, v in m.items()} for m in ms]
        self.state["player_slots"] = {k: list(v) for k, v in ps.items()}
        self.state["pair_count"] = dict(pc)
        self.state["quad_count"] = dict(qc)
        self.state["matchup_count"] = dict(mc)
        self._sync()

    # -- (1) 선수 교환 -------------------------------------------------------
    def _player_swap_plan(self, p1, p2):
        """교환 가능하면 (delta, new_sa, new_sb, pair_changes), 아니면 None."""
        mi1, side1, idx1 = p1
        mi2, side2, idx2 = p2
        if mi1 == mi2:
            return None
        m1, m2 = self.matches[mi1], self.matches[mi2]
        s1, s2 = m1["slot_start"], m2["slot_start"]
        a_id, b_id = m1[side1][idx1], m2[side2][idx2]
        A, B = self.pbid[a_id], self.pbid[b_id]
        if A["gender"] != B["gender"]:
            return None
        if self.multi and A.get("club", "") != B.get("club", ""):
            return None

        sa, sb = self.slots_of[a_id], self.slots_of[b_id]
        if s1 == s2:
            new_sa = new_sb = None          # 같은 시간대 — 시간표는 그대로, 페어/구력만 바뀐다
            delta = 0.0
        else:
            if s2 not in self.avail[a_id] or s1 not in self.avail[b_id]:
                return None
            if s2 in sa or s1 in sb:
                return None
            new_sa = sorted([s for s in sa if s != s1] + [s2])
            new_sb = sorted([s for s in sb if s != s2] + [s1])
            delta = (self.timing(a_id, new_sa) - self.tcache[a_id]
                     + self.timing(b_id, new_sb) - self.tcache[b_id])
            delta += self._finish_delta({a_id: new_sa, b_id: new_sb})

        old_q = self.qcache[mi1] + self.qcache[mi2]
        m1[side1][idx1], m2[side2][idx2] = b_id, a_id
        new_q = self.quality(m1) + self.quality(m2)
        m1[side1][idx1], m2[side2][idx2] = a_id, b_id
        delta += new_q - old_q

        pa = m1[side1][1 - idx1]
        pb = m2[side2][1 - idx2]
        changes = {}
        for k, d in ((pair_key(a_id, pa), -1), (pair_key(b_id, pb), -1),
                     (pair_key(b_id, pa), +1), (pair_key(a_id, pb), +1)):
            changes[k] = changes.get(k, 0) + d
        for k, d in changes.items():
            if not d:
                continue
            old_c = self.state["pair_count"].get(k, 0)
            w = self.hist.get(k, 0.0)
            delta += pair_entry_cost(old_c + d, w) - pair_entry_cost(old_c, w)

        # 두 경기의 출전 4인이 바뀌므로 재대결 카운트도 갱신된다.
        quad_ch, mu_ch = {}, {}
        for m, side, idx, new_id in ((m1, side1, idx1, b_id), (m2, side2, idx2, a_id)):
            old_t1, old_t2 = list(m["team1"]), list(m["team2"])
            new_t1, new_t2 = list(old_t1), list(old_t2)
            (new_t1 if side == "team1" else new_t2)[idx] = new_id
            for kk, dd in ((quad_key(old_t1, old_t2), -1), (quad_key(new_t1, new_t2), +1)):
                quad_ch[kk] = quad_ch.get(kk, 0) + dd
            for kk, dd in ((matchup_key(old_t1, old_t2), -1), (matchup_key(new_t1, new_t2), +1)):
                mu_ch[kk] = mu_ch.get(kk, 0) + dd
        for kk, dd in quad_ch.items():
            if not dd:
                continue
            oc = self.state["quad_count"].get(kk, 0)
            g = _quad_gender(kk, self.pbid)
            fc = bool(g) and self.quad_forced.get(g, False)
            delta += _quad_cost(oc + dd, fc) - _quad_cost(oc, fc)
        for kk, dd in mu_ch.items():
            if not dd:
                continue
            oc = self.state["matchup_count"].get(kk, 0)
            delta += _matchup_cost(oc + dd) - _matchup_cost(oc)

        # 혼복 ↔ 단성 복식 사이의 교환이면 혼복희망 충족도가 바뀐다.
        delta += self._wish_delta(self._mixed_change(m1, m2, a_id, b_id))

        return delta, new_sa, new_sb, (changes, quad_ch, mu_ch)

    def _player_swap_commit(self, p1, p2, new_sa, new_sb, count_changes) -> None:
        changes, quad_ch, mu_ch = count_changes
        mi1, side1, idx1 = p1
        mi2, side2, idx2 = p2
        m1, m2 = self.matches[mi1], self.matches[mi2]
        a_id, b_id = m1[side1][idx1], m2[side2][idx2]
        for pid, ch in self._mixed_change(m1, m2, a_id, b_id).items():
            self.mixed_of[pid] = self.mixed_of.get(pid, 0) + ch
            if self.state.get("player_mixed") is not None:
                self.state["player_mixed"][pid] = self.mixed_of[pid]
        m1[side1][idx1], m2[side2][idx2] = b_id, a_id
        for m in (m1, m2):
            for side in SIDES:
                ids = m[side]
                m[f"{side}_names"] = [self.pbid[x]["name"] for x in ids]
                m[f"{side}_exp_sum"] = self.pbid[ids[0]]["exp"] + self.pbid[ids[1]]["exp"]
        if new_sa is not None:
            self.slots_of[a_id] = new_sa
            self.slots_of[b_id] = new_sb
            self.state["player_slots"][a_id] = list(new_sa)
            self.state["player_slots"][b_id] = list(new_sb)
            self.tcache[a_id] = self.timing(a_id, new_sa)
            self.tcache[b_id] = self.timing(b_id, new_sb)
        for store, ch in (("pair_count", changes), ("quad_count", quad_ch), ("matchup_count", mu_ch)):
            target = self.state[store]
            for k, d in ch.items():
                if not d:
                    continue
                target[k] = target.get(k, 0) + d
                if target[k] <= 0:
                    target.pop(k, None)
        # 같은 성별(교류전이면 같은 클럽)끼리만 교환하므로 pos_groups는 그대로 유효하다.
        self.qcache[mi1] = self.quality(m1)
        self.qcache[mi2] = self.quality(m2)

    # -- (2) 경기 이동 -------------------------------------------------------
    def _match_swap_plan(self, mi1, mi2):
        m1, m2 = self.matches[mi1], self.matches[mi2]
        # 씨드는 '이 시간, 이 코트'에 묶여 있으므로 명단 통째 이동은 아예 막는다.
        if m1.get("pinned") or m2.get("pinned"):
            return None
        s1, s2 = m1["slot_start"], m2["slot_start"]
        if s1 == s2:
            return None
        r1 = m1["team1"] + m1["team2"]
        r2 = m2["team1"] + m2["team2"]
        set1, set2 = set(r1), set(r2)
        movers = [(pid, s1, s2) for pid in r1 if pid not in set2]
        movers += [(pid, s2, s1) for pid in r2 if pid not in set1]
        new_slots, delta = {}, 0.0
        for pid, frm, to in movers:
            if to not in self.avail[pid] or to in self.slots_of[pid]:
                return None
            cur = self.slots_of[pid]
            nxt = sorted([s for s in cur if s != frm] + [to])
            new_slots[pid] = nxt
            delta += self.timing(pid, nxt) - self.tcache[pid]
        delta += self._finish_delta(new_slots)

        old_q = self.qcache[mi1] + self.qcache[mi2]
        saved1 = {f: m1[f] for f in ROSTER_FIELDS}
        saved2 = {f: m2[f] for f in ROSTER_FIELDS}
        for f in ROSTER_FIELDS:
            m1[f], m2[f] = saved2[f], saved1[f]
        new_q = self.quality(m1) + self.quality(m2)
        for f in ROSTER_FIELDS:
            m1[f], m2[f] = saved1[f], saved2[f]
        return delta + new_q - old_q, new_slots

    def _match_swap_commit(self, mi1, mi2, new_slots) -> None:
        m1, m2 = self.matches[mi1], self.matches[mi2]
        saved1 = {f: m1[f] for f in ROSTER_FIELDS}
        saved2 = {f: m2[f] for f in ROSTER_FIELDS}
        for f in ROSTER_FIELDS:
            m1[f], m2[f] = saved2[f], saved1[f]
        for pid, nxt in new_slots.items():
            self.slots_of[pid] = nxt
            self.state["player_slots"][pid] = list(nxt)
            self.tcache[pid] = self.timing(pid, nxt)
        self.qcache[mi1] = self.quality(m1)
        self.qcache[mi2] = self.quality(m2)
        # 명단이 통째로 이동하므로 이 두 경기의 자리에는 성별 구성이 달라질 수 있다 → 후보 묶음 재계산
        self._regroup_positions((mi1, mi2))

    def _regroup_positions(self, match_indices) -> None:
        touched = set(match_indices)
        for key in list(self.pos_groups):
            self.pos_groups[key] = [p for p in self.pos_groups[key] if p[0] not in touched]
        for mi in touched:
            for side in SIDES:
                for idx in (0, 1):
                    if self._is_pinned(mi, side, idx):
                        continue
                    key = self._group_key(self.matches[mi][side][idx])
                    self.pos_groups.setdefault(key, []).append((mi, side, idx))

    # -- 내리막 탐색 ---------------------------------------------------------
    def descend(self, max_passes: int = 12) -> int:
        moves = 0
        for _ in range(max_passes):
            did = 0
            order = list(range(len(self.matches)))
            self.rng.shuffle(order)
            for ii in range(len(order)):
                for jj in range(ii + 1, len(order)):
                    plan = self._match_swap_plan(order[ii], order[jj])
                    if plan and plan[0] < -1e-9:
                        self._match_swap_commit(order[ii], order[jj], plan[1])
                        did += 1
                        break
            for group in self.pos_groups.values():
                if len(group) < 2:
                    continue
                self.rng.shuffle(group)
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        plan = self._player_swap_plan(group[i], group[j])
                        if plan and plan[0] < -1e-9:
                            self._player_swap_commit(group[i], group[j], *plan[1:])
                            did += 1
                            break
            moves += did
            if did == 0:
                break
        return moves

    def _worst_positions(self, top: int = 5):
        """시간표가 가장 나쁜(공백·대기가 큰) 선수들이 들어있는 자리 목록."""
        ranked = sorted(self.tcache, key=lambda pid: -self.tcache[pid])[:top]
        worst = set(ranked)
        return [pos for pos in self.positions if self.matches[pos[0]][pos[1]][pos[2]] in worst]

    def kick(self, strength: int = 2) -> None:
        """무작위 실행 가능 수를 강제로 적용해 국소최적에서 빠져나온다.

        절반 정도는 '공백·대기가 가장 심한 선수'가 낀 경기를 골라 흔든다 —
        정작 고쳐야 할 곳을 건드릴 확률을 높이기 위함.
        """
        n_m, n_p = len(self.matches), len(self.positions)
        if self.rng.random() < 0.5:
            hot = self._worst_positions()
            if hot:
                target = hot[self.rng.randrange(len(hot))]
                key = self._group_key(self.matches[target[0]][target[1]][target[2]])
                group = self.pos_groups.get(key, [])
                for _ in range(40):
                    other = group[self.rng.randrange(len(group))] if group else None
                    if other is None:
                        break
                    plan = self._player_swap_plan(target, other)
                    if plan:
                        self._player_swap_commit(target, other, *plan[1:])
                        break
        for _ in range(strength):
            if self.rng.random() < 0.6 and n_m > 1:
                for _ in range(25):
                    mi1, mi2 = self.rng.randrange(n_m), self.rng.randrange(n_m)
                    if mi1 == mi2:
                        continue
                    plan = self._match_swap_plan(mi1, mi2)
                    if plan:
                        self._match_swap_commit(mi1, mi2, plan[1])
                        break
            elif n_p > 1:
                groups = [g for g in self.pos_groups.values() if len(g) > 1]
                if not groups:
                    continue
                for _ in range(40):
                    g = groups[self.rng.randrange(len(groups))]
                    p1 = g[self.rng.randrange(len(g))]
                    p2 = g[self.rng.randrange(len(g))]
                    plan = self._player_swap_plan(p1, p2)
                    if plan:
                        self._player_swap_commit(p1, p2, *plan[1:])
                        break

    def run(self, kicks: int = 30, max_passes: int = 12) -> float:
        """교란 → 내리막 → 채택 판정을 반복. 최고 기록은 따로 보관한다.

        약간 나빠지는 결과도 초반에는 받아들여(tolerance) 탐색이 한 골짜기에 갇히지 않게 한다.
        """
        self.descend(max_passes)
        cur_score = self.score()
        cur = self.snapshot()
        best_score, best = cur_score, cur
        for k in range(kicks):
            self.kick()
            self.descend(max_passes)
            sc = self.score()
            if sc < best_score - 1e-9:
                best_score, best = sc, self.snapshot()
            tol = G["accept_tolerance"] * (1.0 - k / max(1, kicks))
            if sc <= cur_score + tol:
                cur_score, cur = sc, self.snapshot()
            else:
                self.restore(cur)
        self.restore(best)
        return best_score


def run_one_seed(
    players: list[dict],
    schedule_slots: list[dict],
    seed: int,
    candidate_top_n: int,
    hist_pairs=None,
    couples=None,
    pins=None,
) -> tuple[dict, float]:
    rng = random.Random(seed)
    pin_map, pin_slots, pin_court_of = build_pin_index(pins, players)
    state = init_state(players, hist_pairs, schedule_slots, couples, pin_slots)
    state["pins"] = list(pins or [])   # full_score가 미반영 자리를 세는 데 쓴다

    for slot in schedule_slots:
        played_here = set()
        # 교류전: 클럽 내 게임수 상한 = (이 슬롯에 출전 가능한 같은 클럽 최소 게임수) + SPREAD_CAP.
        # 한 사람이 같은 클럽 동료보다 너무 앞서 가지 못하게 해 클럽 내부 격차를 좁힌다.
        club_floor = {}
        if state.get("multi_club"):
            club_games = {}
            for p in players:
                if slot["slot_start"] in p["available_slots"]:
                    club_games.setdefault(p.get("club", ""), []).append(state["player_games"][p["id"]])
            # floor = 최소 게임수. 단, 상대가 없어 못 뛰는 사람이 floor를 0으로 끌어내려
            # 같은 클럽 동료까지 막지 않도록 (최대-SPREAD_CAP-1) 밑으로는 내려가지 않게 한다.
            for c, gl in club_games.items():
                club_floor[c] = max(min(gl), max(gl) - (SPREAD_CAP + 1))
        # 고정이 있는 코트를 먼저 채운다. 뒤로 미루면 그 경기를 완성할 '이름을 안 적은 자리'의
        # 후보를 앞 코트가 데려가 버려, 입력 검증을 통과한 씨드가 코트만 비우고 끝난다
        # (실측: 여자 4명 슬롯에서 C코트에 2자리만 고정 → 초안 39/40이 C코트 공석).
        # 씨드가 없으면 정렬 키가 모두 같고 sorted는 안정 정렬이라 순서가 바뀌지 않는다.
        for court_name in sorted(slot["courts"],
                                 key=lambda c: (slot["slot_start"], str(c)) not in pin_map):
            pin = pin_map.get((slot["slot_start"], court_name))
            req = pin_ids(pin)
            # 씨드로 지정된 사람은 최대게임수·클럽격차 필터를 통과시킨다(사용자 지정이 우선).
            # 반대로 씨드가 아닌 사람은 '뒤에 남은 고정 자리'만큼 최대게임수 여유를 미리 뺀다.
            pool = [
                p for p in players
                if slot["slot_start"] in p["available_slots"]
                and p["id"] not in played_here
                # 이 슬롯에서 다른 코트에 고정된 사람은 여기 앉히면 안 된다 —
                # 먼저 데려가 버리면 정작 그 코트의 고정 자리를 못 채운다.
                and pin_court_of.get((slot["slot_start"], p["id"]), court_name) == court_name
                and (p["id"] in req
                     or ((p.get("max_games") is None
                          or state["player_games"][p["id"]]
                          + pins_ahead(p["id"], slot["slot_start"], state) < p["max_games"])
                         and (not state.get("multi_club")
                              or state["player_games"][p["id"]]
                              - club_floor.get(p.get("club", ""), 0) < SPREAD_CAP)))
            ]
            if len(pool) < 4:
                continue
            m = pick_match(pool, slot["slot_start"], court_name, state, rng, candidate_top_n, pin=pin)
            if m is None:
                continue
            update_state(state, m)
            played_here.update(m["team1"] + m["team2"])

    return state, full_score(state, players, schedule_slots)


def solve(
    players: list[dict],
    schedule_slots: list[dict],
    seed: int = 7,
    iters: int = 40,
    candidates: int = 24,
    hist_pairs=None,
    refine: int = 6,
    kicks: int = 40,
    progress=None,
    couples=None,
    pins=None,
) -> dict:
    """대진표 생성 전체 절차 (초안 다중 생성 → 상위 초안 로컬 개선 → 최선 선택).

    CLI(main)와 웹(Pyodide run.py)이 공유하는 단일 진입점.
    couples = [[이름1, 이름2, 부부페어 원함], ...] — 혼복 페어 회피/우대 + 종료시각 맞추기.
    pins = 입력 양식 '씨드대진' 시트에서 사용자가 직접 고정한 자리(없으면 None).
    """
    results = []
    for i in range(max(1, iters)):
        s = seed + i
        state, score = run_one_seed(players, schedule_slots, s, candidates, hist_pairs, couples, pins)
        results.append((score, s, state))
        if progress and (i + 1) % 10 == 0:
            progress(f"초안 {i + 1}/{iters}개 생성")

    results.sort(key=lambda x: x[0])
    best_score, best_seed, best_state = results[0]

    # 다듬을 초안 고르기: 점수순으로만 자르면 '복식 구성이 다른' 안(예: 여복 3+혼복 2)이
    # 다듬기 전 거친 점수 때문에 통째로 탈락한다. 로컬 개선은 경기 종류를 못 바꾸므로
    # 구성별로 최소 한 개씩은 남겨 두고, 남는 자리를 점수순으로 채운다.
    def comp_of(entry):
        return (entry[2]["type_count"]["F"], entry[2]["type_count"]["X"])

    picked, seen_comp, taken = [], set(), set()
    # ① 점수 상위 절반은 무조건 확보 (다듬기 효과가 가장 큰 안들)
    for entry in results[: max(1, refine // 2)]:
        picked.append(entry)
        taken.add(entry[1])
        seen_comp.add(comp_of(entry))
    # ② 나머지 자리는 아직 안 나온 구성 중 점수가 가장 좋은 안으로 채운다
    for entry in results:
        if len(picked) >= refine:
            break
        comp = comp_of(entry)
        if comp in seen_comp or entry[1] in taken:
            continue
        seen_comp.add(comp)
        taken.add(entry[1])
        picked.append(entry)
    # ③ 그래도 자리가 남으면 점수순으로 채운다
    for entry in results:
        if len(picked) >= refine:
            break
        if entry[1] not in taken:
            taken.add(entry[1])
            picked.append(entry)

    for n, (score, s, state) in enumerate(picked, 1):
        refiner = Refiner(state, players, schedule_slots, random.Random(s * 7919 + 13))
        new_score = refiner.run(kicks=kicks)
        if new_score < best_score:
            best_state, best_score, best_seed = state, new_score, s
        if progress:
            progress(f"공백·대기 줄이기 {n}/{len(picked)}")

    eff_in = best_state.get("eff_in", {})
    player_stats = []
    for p in players:
        pid = p["id"]
        slots = sorted(best_state["player_slots"][pid])
        gaps = [slots[i] - slots[i - 1] - 30 for i in range(1, len(slots))]
        e_in = eff_in.get(pid, p["in_min"])
        player_stats.append({
            "id": pid,
            "name": p["name"],
            "gender": p["gender"],
            "exp": p["exp"],
            "membership": p["membership"],
            "club": p.get("club", ""),
            "games": best_state["player_games"][pid],
            "available_slots": len(p["available_slots"]),
            "slots_played": slots,
            "in_min": p["in_min"],
            "out_min": p["out_min"],
            "max_games": p.get("max_games"),
            "min_games": p.get("min_games"),
            "mixed_wish": p.get("mixed_wish"),
            "mixed_games": sum(1 for m in best_state["matches"]
                               if m["type"] == "X" and pid in m["team1"] + m["team2"]),
            # 시간표 품질 지표
            "eff_in": e_in,
            "start_delay": (slots[0] - e_in) if slots else 0,
            "gaps": gaps,
            "long_gaps": sum(1 for g in gaps if g >= 60),
            "finish_min": (slots[-1] + 30) if slots else None,
        })

    return {
        "matches": best_state["matches"],
        "player_stats": player_stats,
        "type_count": best_state["type_count"],
        "metadata": {
            "seed": best_seed,
            "score": best_score,
            "iterations": iters,
            "candidates_top_n": candidates,
            "refine_seeds": refine,
            "kicks": kicks,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--iters", type=int, default=40)
    ap.add_argument("--candidates", type=int, default=24)
    ap.add_argument("--refine", type=int, default=6,
                    help="상위 N개 시드에 대해 로컬 개선(선수 교환/경기 이동)을 수행 (0=끔)")
    ap.add_argument("--kicks", type=int, default=40,
                    help="로컬 개선에서 국소최적 탈출용 무작위 교란 횟수")
    ap.add_argument("--history", default="",
                    help="지난주/2주전 페어 회피용 히스토리 JSON (history.py 산출물). 단일 클럽일 때만 반영.")
    args = ap.parse_args()

    with open(args.inp, "r", encoding="utf-8") as f:
        data = json.load(f)

    players = data["players"]
    schedule_slots = data["schedule_slots"]
    couples = data.get("couples") or None
    if couples:
        pref, lst = build_couples(players, couples)
        if lst:
            n_exact = sum(1 for c in lst if c[3])
            print(f"[안내] 부부 페어 반영: 이번 주 참가 부부 {len(lst)}쌍 "
                  f"(혼복 페어 원함 {sum(1 for c in lst if c[2])}쌍"
                  + (f", 종료 30분 차이 {n_exact}쌍" if n_exact else "")
                  + ") — 종료시각 맞추기 포함.")

    hist_pairs = None
    if args.history and os.path.exists(args.history):
        with open(args.history, "r", encoding="utf-8") as f:
            hist_pairs = json.load(f).get("pairs", [])
        distinct_clubs = {p.get("club", "") for p in players if p.get("club", "")}
        if len(distinct_clubs) > 1:
            print(f"[안내] 교류전(클럽 {len(distinct_clubs)}개)이라 지난주 페어 회피는 적용하지 않습니다.")
        else:
            applied = build_hist_penalty(players, hist_pairs)
            print(f"[안내] 지난주 페어 회피 반영: 히스토리 {len(hist_pairs)}쌍 중 이번 주 명단과 겹치는 {len(applied)}쌍 회피 대상.")

    pins = data.get("pins") or None
    if pins:
        n_seats = sum(len(pin_ids(pin)) for pin in pins)
        print(f"[안내] 씨드 대진 반영: 고정 {n_seats}자리 / 경기 {len(pins)}개 — 나머지만 채웁니다.")

    out = solve(
        players, schedule_slots,
        seed=args.seed, iters=args.iters, candidates=args.candidates,
        hist_pairs=hist_pairs, refine=args.refine, kicks=args.kicks,
        couples=couples, pins=pins,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    player_stats = out["player_stats"]
    tc = out["type_count"]
    long_gap_total = sum(s["long_gaps"] for s in player_stats)
    idle_total = sum(sum(g for g in s["gaps"] if g > 0) + s["start_delay"] for s in player_stats)

    print(f"[OK] 대진 생성 완료: {args.out}")
    print(f"  매치: {len(out['matches'])}개  (남복 {tc['M']} / 여복 {tc['F']} / 혼복 {tc['X']})")
    print(f"  베스트 시드: {out['metadata']['seed']}, 점수: {out['metadata']['score']:.2f}")
    games_list = [s["games"] for s in player_stats]
    if games_list:
        print(f"  게임수: min={min(games_list)}, max={max(games_list)}, avg={sum(games_list)/len(games_list):.1f}")
    print(f"  1시간 이상 공백: {long_gap_total}건, 총 대기시간 {idle_total}분")


if __name__ == "__main__":
    main()
