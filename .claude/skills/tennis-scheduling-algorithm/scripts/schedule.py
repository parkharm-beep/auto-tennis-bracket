"""Generate a tennis bracket via multi-seed greedy heuristic + swap local search.

Usage:
    python schedule.py --in <parsed.json> --out <bracket.json> [--seed 42] [--iters 80] [--candidates 24]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys

# ---------------------------------------------------------------------------
# W: 그리디 후보 선택용 가중치 (한 경기를 고를 때의 비용)
# ---------------------------------------------------------------------------
W = dict(
    team_skill_diff=4.0,
    skill_gap_over_tol=40.0,    # 두 팀 구력합 차이가 허용치 초과 (초과분)^2
    pair_repeat=20.0,
    consecutive=3.0,            # 2슬롯 연속은 '몰아서 하고 일찍 끝내기'에 유리 → 약하게만 억제
    three_consec=500.0,
    game_balance=3.0,
    mixed_overuse=5.0,
    mixed_skill_rule_violation=1000.0,
    no_member_guest_mix=1.0,
    court_affinity=25.0,
    female_early_slot=200.0,    # 여성 07:30 이전 슬롯 배정 (1명당) — 사실상 회피
    mixed_nonpriority=120.0,    # 교류전: 단성복식(남복/여복) 우선 — 단, 여복 페어가 반복되면 혼복도 허용
    women_doubles_bonus=200.0,  # 교류전: 여복 우대(우선) — 양 클럽 여자 2+면 여복을 먼저, 다만 절대적이지 않아 혼복도 가능
    history_pair_repeat=30.0,   # 지난주/2주전과 같은 페어 회피(소프트). 우리멤버끼리(단일클럽)일 때만 적용, 교류전 제외.
                                # 같은주 중복페어 유효비용(pair_repeat 20 → 첫 중복 40)보다 낮게 둔다:
                                # 지난주 회피 때문에 이번 주 안에서 같은 짝을 반복하는 자기모순을 막기 위함.
    quad_repeat=95.0,           # 같은 4명이 편만 바꿔 다시 만남 — 이것도 확실히 회피
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
    matchup_repeat=220.0,       # 같은 4명이 상대편까지 그대로 재대결 — 혼복 1~2판보다 나쁘다고 본다
    history_pair=30.0,
    three_streak=400.0,
    two_streak=2.0,
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
    female_early=200.0,
    member_guest=1.0,
    gap=4.0,                    # 경기 사이 공백 30분당
    long_gap=200.0,             # 경기 사이 공백이 1시간 이상일 때 (초과 30분 단위)^2 — 최우선 회피
    # ↑ 같은 4명 재대결(quad_repeat)보다 위. 한 사람이 1시간 노는 게 대진 한 판 겹치는 것보다 나쁘다.
    start_delay=5.0,            # 도착(IN) 후 첫 경기까지 대기 30분당
    long_start_delay=60.0,      # 첫 경기까지 1시간 넘게 기다릴 때 (초과 30분 단위)^2
    idle_sq=5.0,                # 개인별 총 유휴시간(대기+공백)의 볼록 페널티 — 한 사람에게 몰리는 것 방지
    missed=5000.0,
    min_games_short=900.0,      # 최소게임수 미달 (부족분)^2 — 어기지 않는 규칙. balance_under(400)보다 위
    guest_in_mixed=25.0,        # 혼복에 남자 게스트 1명당 — 혼복 남자 자리는 가급적 정회원이 (소프트)
    couple_avoid_pair=250.0,    # '피함' 부부가 혼복 같은 팀 (경기당)
    couple_want_pair=30.0,      # '원함' 부부가 혼복 같은 팀 (경기당 보너스, 음수)
    couple_finish_gap=120.0,    # 부부 마지막 경기 종료 차이가 30분을 넘으면 (초과 30분 단위)^2
                                # 부부는 같이 오가므로 같이 끝나거나 30분 안쪽 차이가 목표 (소프트).
                                # 실측(21명 샘플): 70이면 60분 차가 남고, 200은 공백이 늘어남 — 120이 균형점.
    accept_tolerance=90.0,      # 교란 후 이 정도 나빠짐까지는 받아들여 탐색을 넓힌다(점점 0으로)
)

FEMALE_EARLIEST_SLOT_MIN = 7 * 60 + 30

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


def eff_min_games(p: dict) -> int:
    """이 사람에게 실제로 보장해야 하는 최소 게임수.

    입력의 '최소게임수'를 본인 가용 슬롯 수(그 이상은 물리적으로 불가)로 자른 값.
    최소게임수를 안 적었으면 0.
    """
    mg = p.get("min_games")
    if not mg:
        return 0
    return min(mg, len(p.get("available_slots") or []))


def min_games_critical(p: dict, slot_start: int, state: dict) -> bool:
    """이 슬롯을 놓치면 최소게임수 보장이 깨지는 상태인가 (남은 가용 슬롯 ≤ 부족분)."""
    deficit = eff_min_games(p) - state["player_games"][p["id"]]
    if deficit <= 0:
        return False
    remaining = sum(1 for s in (p.get("available_slots") or []) if s >= slot_start)
    return remaining <= deficit

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

    IN시간이 코트 운영 전이거나(예: 07:00 IN인데 07:00엔 코트가 없음),
    여성처럼 07:30 이전 회피 규칙이 걸린 경우까지 반영한다.
    대기시간을 이 기준으로 재므로 '구조상 불가능한 대기'는 페널티로 잡히지 않는다.
    """
    out = {}
    for p in players:
        av = p.get("available_slots") or []
        base = p["in_min"]
        if p["gender"] == "F":
            base = max(base, FEMALE_EARLIEST_SLOT_MIN)
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


def mixed_limits(players: list[dict], schedule_slots: list[dict] | None,
                 multi: bool = False) -> tuple[int, int]:
    """혼복 경기 수의 (최소 보장, 허용 상한).

    - 남복/여복이 원칙이지만, **여자가 적으면 혼복을 기본으로 1~2판 넣는다.**
      (여자 6명 이하 → 여복만 돌리면 늘 같은 사람끼리라 재미가 없다)
    - 소수 성별이 4~5명이면 중복 없는 단성 복식을 몇 판밖에 못 만든다
      (여자 4명 → 편 가르는 방법이 3가지뿐 → 여복 3판이 한계).
      그 이상 뛰려면 같은 4명이 상대편까지 똑같이 다시 붙어야 하므로,
      모자란 게임수만큼 상한을 더 올린다.
    - 상한을 넘는 혼복은 원칙대로 급격한 페널티.
    """
    if not _mixed_possible(players, multi):
        return 0, 0

    n = len(players)
    n_m = sum(1 for p in players if p["gender"] == "M")
    n_f = n - n_m
    minority = min(n_m, n_f)
    if minority < 4:
        return 0, 10 ** 6       # 단성 복식 자체가 불가능 → 혼복이 유일한 수단

    quota = 0
    minority_players = [p for p in players
                        if p["gender"] == ("F" if n_f <= n_m else "M")]
    # 짝도 안 겹치고 구력도 맞는 단성 복식을 몇 판까지 만들 수 있는지
    capacity = good_singles_capacity(minority_players, multi)
    total_matches = 0
    for sl in (schedule_slots or []):
        n_avail = sum(1 for p in players if sl["slot_start"] in p.get("available_slots", []))
        total_matches += min(len(sl["courts"]), n_avail // 4)
    if total_matches and n:
        target = round(4 * total_matches / n)          # 1인당 공평 게임수
        # 소수 성별이 필요한 총 출전 자리 − 단성 복식이 태울 수 있는 자리(1판당 4명)
        short_seats = max(0, minority * target - 4 * capacity)
        quota = -(-short_seats // 2)                   # 혼복 1판이 소수 성별 2명을 태운다

    if n_f <= MIXED_SMALL_WOMEN:
        return MIXED_MIN_GAMES, max(quota, MIXED_DEFAULT_QUOTA)
    return 0, quota


def build_couples(players: list[dict], couples) -> tuple[dict, list]:
    """이름 기준 부부 목록([[이름1, 이름2, want], ...]) → 이번 주 id 기준 매핑.

    둘 다 이번 주 참가자일 때만 반영. 반환: (pair_key→want, [(ida, idb, want), ...])
    """
    name_to_id = {p["name"]: p["id"] for p in players}
    pref, lst = {}, []
    for entry in couples or []:
        if not entry or len(entry) < 2:
            continue
        a, b = str(entry[0]), str(entry[1])
        want = bool(entry[2]) if len(entry) > 2 else False
        ida, idb = name_to_id.get(a), name_to_id.get(b)
        if ida and idb and ida != idb:
            pref[pair_key(ida, idb)] = want
            lst.append((ida, idb, want))
    return pref, lst


def couple_finish_cost(last_a, last_b) -> float:
    """부부 두 사람의 마지막 경기 슬롯 차이 비용. 30분 이내는 0, 그 이상은 제곱 페널티."""
    if last_a is None or last_b is None:
        return 0.0
    u = abs(last_a - last_b) / 30.0 - 1.0
    return G["couple_finish_gap"] * u * u if u > 0 else 0.0


def init_state(players: list[dict], hist_pairs=None, schedule_slots=None, couples=None) -> dict:
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
    return {
        # 부부 페어: 혼복 같은 팀 회피(피함)/우대(원함) + 종료 시각 맞추기용
        "couple_pref": couple_pref,
        "couples": couple_list,
        "matches": [],
        "player_games": {p["id"]: 0 for p in players},
        "player_slots": {p["id"]: [] for p in players},
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
    }


def update_state(state: dict, match: dict) -> None:
    state["matches"].append(match)
    n_f = 2 if match["type"] == "X" else (4 if match["type"] == "F" else 0)
    state["gender_sum"]["F"] += n_f
    state["gender_sum"]["M"] += 4 - n_f
    for p_id in match["team1"] + match["team2"]:
        state["player_games"][p_id] += 1
        state["player_slots"][p_id].append(match["slot_start"])
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
    # (여자가 4명뿐이면 여복 4판째에 반드시 발생 → 이때 혼복이 차선책으로 열린다)
    t1_ids = (team1[0]["id"], team1[1]["id"])
    t2_ids = (team2[0]["id"], team2[1]["id"])
    q_prev = state["quad_count"].get(quad_key(t1_ids, t2_ids), 0)
    if q_prev > 0:
        cost += W["quad_repeat"] * q_prev
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
        if is_two_streak(p["id"], slot_start, state):
            cost += W["consecutive"]
        if is_three_streak(p["id"], slot_start, state):
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

        # 최소게임수 미달자는 우대 투입. 남은 가용 슬롯이 부족분만큼밖에 없으면
        # 이 슬롯을 놓치는 순간 보장이 깨지므로 사실상 강제로 태운다.
        deficit = eff_min_games(p) - played
        if deficit > 0:
            cost -= W["min_games_deficit"] * deficit
            remaining = sum(1 for s in (p.get("available_slots") or []) if s >= slot_start)
            if remaining <= deficit:
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
        # (여자 게스트는 혼복 가능)
        cost += W["guest_in_mixed"] * sum(
            1 for p in all_players if p["gender"] == "M" and p["membership"] == "게스트")
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

    if slot_start < FEMALE_EARLIEST_SLOT_MIN:
        female_count = sum(1 for p in all_players if p["gender"] == "F")
        if female_count > 0:
            cost += W["female_early_slot"] * female_count

    return cost


def enumerate_candidates(
    pool: list[dict],
    slot_start: int,
    state: dict,
    rng: random.Random,
    top_k: int = 10,
    court_name: str = "",
) -> list[tuple[float, str, tuple, tuple]]:
    # 3연속 위험자 분리. 풀이 충분하면 안전 풀만 사용.
    # (최소게임수 보장이 위태로운 사람은 3연속이라도 안전 풀에 남긴다)
    safe_pool = [p for p in pool
                 if not is_three_streak(p["id"], slot_start, state)
                 or min_games_critical(p, slot_start, state)]
    if len(safe_pool) >= 4:
        working_pool = safe_pool
    else:
        risky = [p for p in pool if is_three_streak(p["id"], slot_start, state)]
        working_pool = safe_pool + risky

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
        mixed_is_fallback = not (
            clean_singles_available([p for p in pool_sorted if p["gender"] == "M"], state)
            and clean_singles_available([p for p in pool_sorted if p["gender"] == "F"], state)
        )
        for i in range(len(club_keys)):
            for j in range(i + 1, len(club_keys)):
                Am, Bm = m_by_club.get(club_keys[i], []), m_by_club.get(club_keys[j], [])
                Af, Bf = f_by_club.get(club_keys[i], []), f_by_club.get(club_keys[j], [])
                for pa in itertools.combinations(Am, 2):       # 남복: i클럽 vs j클럽
                    for pb in itertools.combinations(Bm, 2):
                        candidates.append((match_cost(pa, pb, "M", slot_start, state, pool_m, pool_f, court_name), "M", pa, pb))
                for pa in itertools.combinations(Af, 2):       # 여복
                    for pb in itertools.combinations(Bf, 2):
                        candidates.append((match_cost(pa, pb, "F", slot_start, state, pool_m, pool_f, court_name), "F", pa, pb))
                for am in Am:                                  # 혼복: (남1+여1) vs (남1+여1)
                    for af in Af:
                        t1 = (am, af)
                        for bm in Bm:
                            for bf in Bf:
                                t2 = (bm, bf)
                                candidates.append((match_cost(t1, t2, "X", slot_start, state, pool_m, pool_f,
                                                              court_name, mixed_is_fallback), "X", t1, t2))
        return candidates

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

    if len(singles_m) >= 4:
        for combo in itertools.combinations(singles_m, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "M", slot_start, state, pool_m, pool_f, court_name)
                candidates.append((c, "M", t1, t2))

    if len(singles_f) >= 4:
        for combo in itertools.combinations(singles_f, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "F", slot_start, state, pool_m, pool_f, court_name)
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

    return candidates


SPLITS_OF_4 = (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)))


def clean_singles_available(same_gender_pool: list[dict], state: dict, limit: int = 8) -> bool:
    """이 성별만으로 '쓸 만한' 단성 복식을 아직 만들 수 있는가.

    쓸 만하다 = ① 같은 짝이 처음이고 ② 같은 4명이 다시 만나는 것도 아니고
              ③ 두 팀 구력합 차이가 허용치 이내(전원 10년 미만이면 3, 10년 이상 포함이면 4).

    False면 지금 단성 복식을 짜봐야 짝이 겹치거나, 같은 사람들끼리 또 붙거나,
    구력이 안 맞는 재미없는 경기가 된다는 뜻 → 혼복이 정당한 차선책이 된다.
    (여자가 4명뿐이면 편 가르는 방법이 3가지뿐이라 금방 False가 된다)
    """
    people = same_gender_pool[:limit]
    if len(people) < 4:
        return False
    pc, mc, qc = state["pair_count"], state["matchup_count"], state["quad_count"]
    multi = bool(state.get("multi_club"))
    for combo in itertools.combinations(people, 4):
        if qc.get(quad_key((combo[0]["id"], combo[1]["id"]), (combo[2]["id"], combo[3]["id"]))):
            continue                                # 이 4명은 이미 한 번 붙었다
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
    pool_size: int,
) -> list[tuple]:
    def _streak_blocked(p):
        """3연속 출전으로 걸러야 하는 선수인가.

        단, 최소게임수 보장이 위태로운 사람은 예외 — 지금 안 태우면 보장이
        깨지므로 3연속이라도 태운다.
        """
        return (is_three_streak(p["id"], slot_start, state)
                and not min_games_critical(p, slot_start, state))

    filtered = []
    for entry in cands:
        cost, mtype, t1, t2 = entry
        all_players = list(t1) + list(t2)

        if pool_size >= 8:
            if any(_streak_blocked(p) for p in all_players):
                continue

        filtered.append(entry)
    return filtered


def pick_match(
    pool: list[dict],
    slot_start: int,
    court: str,
    state: dict,
    rng: random.Random,
    candidate_top_n: int = 24,
) -> dict | None:
    cands = enumerate_candidates(pool, slot_start, state, rng, court_name=court)
    if not cands:
        return None
    filtered = _hard_filter(cands, slot_start, state, len(pool))
    cands = filtered if filtered else cands
    cands.sort(key=lambda x: x[0])
    pick_pool = cands[: min(len(cands), candidate_top_n)]
    weights = [1.0 / (1.0 + i) ** 2 for i in range(len(pick_pool))]
    chosen = rng.choices(pick_pool, weights=weights, k=1)[0]
    _, mtype, t1, t2 = chosen
    return {
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


# ---------------------------------------------------------------------------
# 전체 대진표 평가 (시드 선택 + 로컬 개선 공통 목적함수)
# ---------------------------------------------------------------------------

def player_timing_cost(slots_sorted: list[int], eff_in: int) -> float:
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

    for i in range(len(slots_sorted)):
        if i >= 2 and slots_sorted[i - 1] == slots_sorted[i] - 30 and slots_sorted[i - 2] == slots_sorted[i] - 60:
            cost += G["three_streak"]
        elif i >= 1 and slots_sorted[i - 1] == slots_sorted[i] - 30:
            cost += G["two_streak"]

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

    if match["slot_start"] < FEMALE_EARLIEST_SLOT_MIN:
        cost += G["female_early"] * sum(1 for p in t1 + t2 if p["gender"] == "F")

    for team in (t1, t2):
        if team[0]["membership"] == team[1]["membership"]:
            cost += G["member_guest"]

    return cost


def _quad_cost(count: int) -> float:
    return G["quad_repeat"] * (count - 1) if count > 1 else 0.0


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
        score += player_timing_cost(sorted(state["player_slots"][pid]), eff_in.get(pid, p["in_min"]))

    # 페어 중복 + 지난 대진표 반복
    for k, c in state["pair_count"].items():
        score += pair_entry_cost(c, hist.get(k, 0.0))

    # 같은 4명 재대결 (편 구성 무관 / 상대편까지 그대로)
    for c in state["quad_count"].values():
        score += _quad_cost(c)
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

    # 부부는 같이 오간다 — 마지막 경기 종료가 같거나 30분 안쪽 차이가 되게 (소프트)
    for ida, idb, _w in state.get("couples", []):
        sa = state["player_slots"].get(ida) or []
        sb = state["player_slots"].get(idb) or []
        score += couple_finish_cost(max(sa) if sa else None, max(sb) if sb else None)

    # 비어버린 코트-슬롯
    feasible_court_slots = 0
    for sl in schedule_slots:
        n_courts = len(sl["courts"])
        n_avail = sum(1 for p in players if sl["slot_start"] in p["available_slots"])
        feasible_court_slots += min(n_courts, n_avail // 4)
    score += max(0, feasible_court_slots - len(state["matches"])) * G["missed"]

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
        for ida, idb, _w in state.get("couples", []):
            self.partner[ida] = idb
            self.partner[idb] = ida
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
            delta += couple_finish_cost(new_a, new_b) - couple_finish_cost(old_a, old_b)
        return delta

    # -- 파생 캐시 -----------------------------------------------------------
    def _sync(self) -> None:
        self.matches = self.state["matches"]
        self.slots_of = {pid: sorted(sl) for pid, sl in self.state["player_slots"].items()}
        self.positions = [
            (mi, side, idx)
            for mi in range(len(self.matches))
            for side in SIDES
            for idx in (0, 1)
        ]
        # 교환 후보는 '같은 성별(교류전이면 같은 클럽)'끼리만 가능하므로 미리 묶어 둔다.
        self.pos_groups = {}
        for pos in self.positions:
            mi, side, idx = pos
            self.pos_groups.setdefault(self._group_key(self.matches[mi][side][idx]), []).append(pos)
        self.qcache = [self.quality(m) for m in self.matches]
        self.tcache = {pid: self.timing(pid, sl) for pid, sl in self.slots_of.items()}

    def _group_key(self, pid):
        p = self.pbid[pid]
        return (p["gender"], p.get("club", "")) if self.multi else p["gender"]

    def timing(self, pid, slots) -> float:
        return player_timing_cost(slots, self.eff_in.get(pid, self.pbid[pid]["in_min"]))

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
            delta += _quad_cost(oc + dd) - _quad_cost(oc)
        for kk, dd in mu_ch.items():
            if not dd:
                continue
            oc = self.state["matchup_count"].get(kk, 0)
            delta += _matchup_cost(oc + dd) - _matchup_cost(oc)

        return delta, new_sa, new_sb, (changes, quad_ch, mu_ch)

    def _player_swap_commit(self, p1, p2, new_sa, new_sb, count_changes) -> None:
        changes, quad_ch, mu_ch = count_changes
        mi1, side1, idx1 = p1
        mi2, side2, idx2 = p2
        m1, m2 = self.matches[mi1], self.matches[mi2]
        a_id, b_id = m1[side1][idx1], m2[side2][idx2]
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
) -> tuple[dict, float]:
    rng = random.Random(seed)
    state = init_state(players, hist_pairs, schedule_slots, couples)

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
        for court_name in slot["courts"]:
            pool = [
                p for p in players
                if slot["slot_start"] in p["available_slots"]
                and p["id"] not in played_here
                and (p.get("max_games") is None or state["player_games"][p["id"]] < p["max_games"])
                and (not state.get("multi_club")
                     or state["player_games"][p["id"]] - club_floor.get(p.get("club", ""), 0) < SPREAD_CAP)
            ]
            if len(pool) < 4:
                continue
            m = pick_match(pool, slot["slot_start"], court_name, state, rng, candidate_top_n)
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
) -> dict:
    """대진표 생성 전체 절차 (초안 다중 생성 → 상위 초안 로컬 개선 → 최선 선택).

    CLI(main)와 웹(Pyodide run.py)이 공유하는 단일 진입점.
    couples = [[이름1, 이름2, 부부페어 원함], ...] — 혼복 페어 회피/우대 + 종료시각 맞추기.
    """
    results = []
    for i in range(max(1, iters)):
        s = seed + i
        state, score = run_one_seed(players, schedule_slots, s, candidates, hist_pairs, couples)
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
            print(f"[안내] 부부 페어 반영: 이번 주 참가 부부 {len(lst)}쌍 "
                  f"(혼복 페어 원함 {sum(1 for _, _, w in lst if w)}쌍) — 종료시각 30분 이내 맞추기 포함.")

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

    out = solve(
        players, schedule_slots,
        seed=args.seed, iters=args.iters, candidates=args.candidates,
        hist_pairs=hist_pairs, refine=args.refine, kicks=args.kicks,
        couples=couples,
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
