"""Generate a tennis bracket via multi-seed greedy heuristic.

Usage:
    python schedule.py --in <parsed.json> --out <bracket.json> [--seed 42] [--iters 80] [--candidates 24]
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import random
import sys

W = dict(
    team_skill_diff=4.0,
    pair_repeat=20.0,
    consecutive=5.0,
    three_consec=500.0,
    game_balance=2.0,
    mixed_overuse=5.0,
    mixed_skill_rule_violation=1000.0,
    no_member_guest_mix=1.0,
    court_affinity=25.0,
    female_early_slot=80.0,
)

FEMALE_EARLIEST_SLOT_MIN = 7 * 60 + 30

COURT_AFFINITY = {
    "A": {"M": 1.0, "F": 0.0, "X": 0.0},
    "B": {"M": 0.0, "F": 1.0, "X": 1.0},
    "C": {"M": 0.0, "F": 0.0, "X": 0.0},
}


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _same_club(a: dict, b: dict) -> bool:
    """두 선수가 같은 클럽 소속인지. 클럽 정보 없으면 같은 것으로 간주."""
    return a.get("club", "") == b.get("club", "")


def init_state(players: list[dict]) -> dict:
    distinct_clubs = {p.get("club", "") for p in players if p.get("club", "")}
    club_members = {}
    for p in players:
        club_members.setdefault(p.get("club", ""), []).append(p["id"])
    return {
        "matches": [],
        "player_games": {p["id"]: 0 for p in players},
        "player_slots": {p["id"]: [] for p in players},
        "pair_count": {},
        "type_count": {"M": 0, "F": 0, "X": 0},
        # 클럽이 2개 이상이면 교류전 모드 — 상대 팀은 반드시 다른 클럽(하드), 게임수 균형은 클럽 내부 기준.
        "multi_club": len(distinct_clubs) > 1,
        "club_of": {p["id"]: p.get("club", "") for p in players},
        "club_members": club_members,
        "club_count": {c: len(ids) for c, ids in club_members.items()},
        "club_game_sum": {c: 0 for c in club_members},
    }


def update_state(state: dict, match: dict) -> None:
    state["matches"].append(match)
    for p_id in match["team1"] + match["team2"]:
        state["player_games"][p_id] += 1
        state["player_slots"][p_id].append(match["slot_start"])
        club = state.get("club_of", {}).get(p_id, "")
        if club in state.get("club_game_sum", {}):
            state["club_game_sum"][club] += 1
    for team in (match["team1"], match["team2"]):
        k = pair_key(team[0], team[1])
        state["pair_count"][k] = state["pair_count"].get(k, 0) + 1
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
) -> float:
    cost = 0.0
    all_players = list(team1) + list(team2)

    t1_exp = team1[0]["exp"] + team1[1]["exp"]
    t2_exp = team2[0]["exp"] + team2[1]["exp"]
    cost += W["team_skill_diff"] * abs(t1_exp - t2_exp)

    for team in (team1, team2):
        k = pair_key(team[0]["id"], team[1]["id"])
        prev = state["pair_count"].get(k, 0)
        if prev > 0:
            cost += W["pair_repeat"] * (prev * prev + 1)

    for p in all_players:
        if is_two_streak(p["id"], slot_start, state):
            cost += W["consecutive"]
        if is_three_streak(p["id"], slot_start, state):
            cost += W["three_consec"]

    if state.get("multi_club"):
        # 교류전: 게임수 균형은 각 클럽 '내부'에서만 평가 (클럽 간 차이는 허용)
        for p in all_players:
            club = p.get("club", "")
            cnt = state.get("club_count", {}).get(club, 1)
            club_avg = state.get("club_game_sum", {}).get(club, 0) / max(1, cnt)
            new_g = state["player_games"][p["id"]] + 1
            cost += W["game_balance"] * (new_g - club_avg) ** 2
    elif state["player_games"]:
        avg_games = sum(state["player_games"].values()) / max(1, len(state["player_games"]))
        for p in all_players:
            new_g = state["player_games"][p["id"]] + 1
            cost += W["game_balance"] * (new_g - avg_games) ** 2

    if match_type == "X":
        if pool_males_count >= 4 or pool_females_count >= 4:
            cost += W["mixed_overuse"]
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
    safe_pool = [p for p in pool if not is_three_streak(p["id"], slot_start, state)]
    if len(safe_pool) >= 4:
        working_pool = safe_pool
    else:
        risky = [p for p in pool if is_three_streak(p["id"], slot_start, state)]
        working_pool = safe_pool + risky

    pool_sorted = sorted(
        working_pool,
        key=lambda p: (
            state["player_games"][p["id"]],
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
                                candidates.append((match_cost(t1, t2, "X", slot_start, state, pool_m, pool_f, court_name), "X", t1, t2))
        return candidates

    # 단일 클럽(평소): 기존 로직
    top = pool_sorted[: min(len(pool_sorted), top_k)]
    males = [p for p in top if p["gender"] == "M"]
    females = [p for p in top if p["gender"] == "F"]

    if len(males) >= 4:
        for combo in itertools.combinations(males, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "M", slot_start, state, pool_m, pool_f, court_name)
                candidates.append((c, "M", t1, t2))

    if len(females) >= 4:
        for combo in itertools.combinations(females, 4):
            splits = [
                ((combo[0], combo[1]), (combo[2], combo[3])),
                ((combo[0], combo[2]), (combo[1], combo[3])),
                ((combo[0], combo[3]), (combo[1], combo[2])),
            ]
            for t1, t2 in splits:
                c = match_cost(t1, t2, "F", slot_start, state, pool_m, pool_f, court_name)
                candidates.append((c, "F", t1, t2))

    if len(males) >= 2 and len(females) >= 2:
        for m_combo in itertools.combinations(males, 2):
            for f_combo in itertools.combinations(females, 2):
                for swap in (False, True):
                    if not swap:
                        t1 = (m_combo[0], f_combo[0])
                        t2 = (m_combo[1], f_combo[1])
                    else:
                        t1 = (m_combo[0], f_combo[1])
                        t2 = (m_combo[1], f_combo[0])
                    c = match_cost(t1, t2, "X", slot_start, state, pool_m, pool_f, court_name)
                    candidates.append((c, "X", t1, t2))

    return candidates


def _hard_filter(
    cands: list[tuple],
    slot_start: int,
    state: dict,
    pool_size: int,
) -> list[tuple]:
    filtered = []
    for entry in cands:
        cost, mtype, t1, t2 = entry
        all_players = list(t1) + list(t2)

        if pool_size >= 8:
            if any(is_three_streak(p["id"], slot_start, state) for p in all_players):
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


def run_one_seed(
    players: list[dict],
    schedule_slots: list[dict],
    seed: int,
    candidate_top_n: int,
) -> tuple[dict, float]:
    rng = random.Random(seed)
    state = init_state(players)
    players_by_id = {p["id"]: p for p in players}

    for slot in schedule_slots:
        played_here = set()
        for court_name in slot["courts"]:
            pool = [
                p for p in players
                if slot["slot_start"] in p["available_slots"]
                and p["id"] not in played_here
                and (p.get("max_games") is None or state["player_games"][p["id"]] < p["max_games"])
            ]
            if len(pool) < 4:
                continue
            m = pick_match(pool, slot["slot_start"], court_name, state, rng, candidate_top_n)
            if m is None:
                continue
            update_state(state, m)
            played_here.update(m["team1"] + m["team2"])

    score = 0.0
    if state.get("multi_club"):
        # 교류전: 클럽 '내부'에서만 게임수 균형을 평가 (클럽 간 차이는 허용)
        for ids in state["club_members"].values():
            gs = [state["player_games"][i] for i in ids]
            if gs:
                avg = sum(gs) / len(gs)
                score += sum((g - avg) ** 2 for g in gs) * 5.0
                score += (max(gs) - min(gs)) * 10.0
    else:
        games = list(state["player_games"].values())
        if games:
            avg = sum(games) / len(games)
            score += sum((g - avg) ** 2 for g in games) * 5.0
            score += (max(games) - min(games)) * 10.0

    pair_dups = sum(c - 1 for c in state["pair_count"].values() if c > 1)
    score += pair_dups * 30.0

    three_streak = 0
    two_streak = 0
    for p_id, slots in state["player_slots"].items():
        slots_sorted = sorted(slots)
        for i, s in enumerate(slots_sorted):
            if i >= 2 and slots_sorted[i - 1] == s - 30 and slots_sorted[i - 2] == s - 60:
                three_streak += 1
            elif i >= 1 and slots_sorted[i - 1] == s - 30:
                two_streak += 1
    score += three_streak * 200.0
    score += two_streak * 2.0

    total = sum(state["type_count"].values())
    if total > 0:
        mixed_ratio = state["type_count"]["X"] / total
        score += mixed_ratio * 20.0

    feasible_court_slots = 0
    for sl in schedule_slots:
        n_courts = len(sl["courts"])
        n_avail = sum(1 for p in players if sl["slot_start"] in p["available_slots"])
        feasible_court_slots += min(n_courts, n_avail // 4)
    actual_matches = len(state["matches"])
    missed = max(0, feasible_court_slots - actual_matches)
    score += missed * 5000.0

    return state, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--iters", type=int, default=80)
    ap.add_argument("--candidates", type=int, default=24)
    args = ap.parse_args()

    with open(args.inp, "r", encoding="utf-8") as f:
        data = json.load(f)

    players = data["players"]
    schedule_slots = data["schedule_slots"]

    best_state, best_score = None, float("inf")
    best_seed = args.seed
    for i in range(args.iters):
        seed = args.seed + i
        state, score = run_one_seed(players, schedule_slots, seed, args.candidates)
        if score < best_score:
            best_state, best_score = state, score
            best_seed = seed

    if best_state is None:
        print("[에러] 대진 생성 실패: 가용 인원 부족", file=sys.stderr)
        sys.exit(1)

    name_by_id = {p["id"]: p["name"] for p in players}
    player_stats = []
    for p in players:
        pid = p["id"]
        slots = sorted(best_state["player_slots"][pid])
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
        })

    out = {
        "matches": best_state["matches"],
        "player_stats": player_stats,
        "type_count": best_state["type_count"],
        "metadata": {
            "seed": best_seed,
            "score": best_score,
            "iterations": args.iters,
            "candidates_top_n": args.candidates,
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] 대진 생성 완료: {args.out}")
    print(f"  매치: {len(best_state['matches'])}개  (남복 {best_state['type_count']['M']} / 여복 {best_state['type_count']['F']} / 혼복 {best_state['type_count']['X']})")
    print(f"  베스트 시드: {best_seed}, 점수: {best_score:.2f}")
    games_list = [s["games"] for s in player_stats]
    if games_list:
        print(f"  게임수: min={min(games_list)}, max={max(games_list)}, avg={sum(games_list)/len(games_list):.1f}")


if __name__ == "__main__":
    main()
