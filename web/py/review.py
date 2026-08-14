"""Review the quality of a generated tennis bracket.

Usage:
    python review.py --parsed <01_parsed.json> --bracket <02_bracket.json> --out <03_review.json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict


THRESHOLDS = dict(
    game_gap_global=4,
    game_gap_group=2,
    pair_dup_rate=0.05,
    three_consec_per_player=0,  # 3연속 출전은 하드 금지 — 1건이라도 나오면 RETRY (알고리즘 버그)
    team_skill_avg=3.0,
    team_skill_max=7,
    mixed_skill_violations=0,
    mixed_ratio_balanced=0.25,
    long_gap_min=60,        # 경기 사이 이 시간 이상 비면 '긴 공백'
    long_gap_rate=0.30,     # 참가자 대비 긴 공백 발생 비율이 이보다 크면 재시도
    long_gap_max_per_player=1,
)


def min_to_hhmm(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def max_games_no3(slots: list) -> int:
    """이 슬롯들에서 '3연속 출전 금지'를 지키며 뛸 수 있는 최대 게임수 (schedule.py와 동일 기준)."""
    b0, b1, b2 = 0, None, None
    prev = None
    for s in sorted(slots):
        adj = prev is not None and s - prev == 30
        rest_best = max(x for x in (b0, b1, b2) if x is not None)
        if adj:
            n1, n2 = b0 + 1, (b1 + 1 if b1 is not None else None)
        else:
            n1, n2 = rest_best + 1, None
        b0, b1, b2 = rest_best, n1, n2
        prev = s
    return max(x for x in (b0, b1, b2) if x is not None)


def compute_scores(parsed: dict, bracket: dict, hist_pairs=None) -> dict:
    matches = bracket["matches"]
    player_stats = bracket["player_stats"]
    players_by_id = {p["id"]: p for p in parsed["players"]}

    scores = {}
    issues = []

    distinct_clubs = {p.get("club", "") for p in parsed["players"] if p.get("club", "")}
    is_exchange = len(distinct_clubs) > 1

    games = [s["games"] for s in player_stats]
    scores["game_gap_global"] = (max(games) - min(games)) if games else 0
    scores["games_min"] = min(games) if games else 0
    scores["games_max"] = max(games) if games else 0
    scores["games_avg"] = round(sum(games) / len(games), 2) if games else 0

    # 교류전: 게임수 격차는 (클럽, 성별) 그룹 내부 기준으로 평가 (클럽 간/성별 간 차이는 허용)
    within_gaps = []
    if is_exchange:
        gg = defaultdict(list)
        for s in player_stats:
            gg[(s.get("club", ""), s.get("gender"))].append(s["games"])
        for vs in gg.values():
            if vs:
                within_gaps.append(max(vs) - min(vs))
    scores["game_gap_within_club"] = max(within_gaps, default=0)

    groups = defaultdict(list)
    for s in player_stats:
        # 개인별 최대/최소 게임수를 지정한 사람은 의도적으로 게임수가 다르므로 균형 비교에서 제외
        if s.get("max_games") is not None or s.get("min_games"):
            continue
        key = (s.get("club", ""), s.get("gender"), s["available_slots"]) if is_exchange else s["available_slots"]
        groups[key].append(s)
    group_gaps = []
    for grp_key, members in groups.items():
        if len(members) < 2:
            continue
        gs = [m["games"] for m in members]
        gap = max(gs) - min(gs)
        group_gaps.append((grp_key, gap))
        if gap > THRESHOLDS["game_gap_group"]:
            if is_exchange and isinstance(grp_key, tuple):
                label = f"{grp_key[0]}/{grp_key[1]} · 가용슬롯 {grp_key[2]}개"
            else:
                label = f"가용슬롯 {grp_key}개"
            issues.append({
                "severity": "high",
                "code": "game_gap_group",
                "msg": f"{label} 그룹 내 게임수 격차 {gap} (임계 {THRESHOLDS['game_gap_group']} 초과)",
            })
    scores["group_gaps"] = group_gaps
    scores["max_group_gap"] = max((g for _, g in group_gaps), default=0)

    max_games_violations = []
    for s in player_stats:
        if s.get("max_games") is not None and s["games"] > s["max_games"]:
            max_games_violations.append(s["name"])
            issues.append({
                "severity": "high",
                "code": "max_games_violation",
                "msg": f"{s['name']}: 최대게임수 {s['max_games']} 초과 — 실제 {s['games']}게임 배정됨 (알고리즘 버그)",
            })
    scores["max_games_violations"] = max_games_violations

    # 최소게임수 보장 — '3연속 금지를 지키며 뛸 수 있는 최대 게임수' 한도로 자른 값 기준
    # (schedule.eff_min_games와 동일). 미달이면 RETRY.
    min_games_violations = []
    for s in player_stats:
        mg = s.get("min_games")
        if not mg:
            continue
        cap = max_games_no3(players_by_id.get(s["id"], {}).get("available_slots") or [])
        eff = min(mg, cap)
        if s["games"] < eff:
            min_games_violations.append(s["name"])
            issues.append({
                "severity": "high",
                "code": "min_games_violation",
                "msg": f"{s['name']}: 최소게임수 {mg} 미달 — 실제 {s['games']}게임 배정됨 (보장 {eff}게임)",
            })
        if mg > cap:
            issues.append({
                "severity": "low",
                "code": "min_games_capped",
                "msg": f"{s['name']}: 최소게임수 {mg} > 3연속 없이 가능한 최대 {cap}게임 — {eff}게임까지만 보장 (입력 구조상 한계)",
            })
    scores["min_games_violations"] = min_games_violations

    # 혼복희망 — 적은 사람만 본다. 미달은 medium(정보·소프트, RETRY 아님):
    # 혼복 판수 자체가 남복/여복 우선·구력 균형 같은 하드한 것들에 밀릴 수 있다.
    mixed_wish_short = []
    # 혼복 자체가 성립 불가능한 명단(한쪽 성별 0명 / 교류전에서 남1+여1 팀을 못 만드는 클럽)인지.
    # '구조적으로 불가능'과 '경쟁에서 밀림'은 원인이 달라 문구를 나눈다.
    _clubs = {p.get("club", "") for p in parsed["players"]}
    if len(_clubs) > 1:
        mixed_possible = any(
            any(p["gender"] == "M" and p.get("club", "") == c for p in parsed["players"])
            and any(p["gender"] == "F" and p.get("club", "") == c for p in parsed["players"])
            for c in _clubs)
    else:
        mixed_possible = (any(p["gender"] == "M" for p in parsed["players"])
                          and any(p["gender"] == "F" for p in parsed["players"]))
    for s in player_stats:
        w = s.get("mixed_wish")
        if not w:
            continue
        got = s.get("mixed_games", 0)
        if got < w:
            mixed_wish_short.append(f"{s['name']} {got}/{w}")
            why = ("이번 주 인원 구성으로는 혼복 자체가 불가능"
                   if not mixed_possible else "혼복 판수가 부족했음")
            issues.append({
                "severity": "medium",
                "code": "mixed_wish_short",
                "msg": f"{s['name']}: 혼복희망 {w}게임 중 {got}게임만 배정 — {why}",
            })
    scores["mixed_wish_short"] = mixed_wish_short

    pair_count = defaultdict(int)
    for m in matches:
        for team in (m["team1"], m["team2"]):
            pair_count[pair_key(team[0], team[1])] += 1
    total_pairs = len(pair_count) if pair_count else 1
    dup_pairs = [k for k, v in pair_count.items() if v >= 2]
    scores["pair_dup_count"] = len(dup_pairs)
    scores["pair_dup_rate"] = round(len(dup_pairs) / total_pairs, 4)
    for k in dup_pairs:
        n1 = players_by_id[k[0]]["name"]
        n2 = players_by_id[k[1]]["name"]
        cnt = pair_count[k]
        issues.append({
            "severity": "medium" if cnt == 2 else "high",
            "code": "pair_repeat",
            "msg": f"페어 중복: {n1}+{n2} ({cnt}회)",
        })

    # 같은 4명 재대결: 편만 바꿔 다시(quad) / 상대편까지 그대로(matchup)
    quad_count, matchup_count = defaultdict(int), defaultdict(int)
    for m in matches:
        t1, t2 = tuple(sorted(m["team1"])), tuple(sorted(m["team2"]))
        quad_count[tuple(sorted(t1 + t2))] += 1
        matchup_count[(t1, t2) if t1 < t2 else (t2, t1)] += 1
    scores["quad_repeats"] = sum(c - 1 for c in quad_count.values() if c > 1)
    scores["matchup_repeats"] = sum(c - 1 for c in matchup_count.values() if c > 1)
    for k, c in matchup_count.items():
        if c > 1:
            names = " / ".join(
                "+".join(players_by_id[i]["name"] for i in team) for team in k
            )
            issues.append({
                "severity": "high",
                "code": "matchup_repeat",
                "msg": f"완전 중복 대진: {names} — 같은 4명이 같은 편으로 {c}회 (인원 사정상 불가피할 수 있음)",
            })

    # 지난주/2주전 대비 반복 페어 (정보용 — 우리멤버끼리일 때만). RETRY 사유 아님.
    hist_name_set = {pair_key(str(e[0]), str(e[1])) for e in (hist_pairs or []) if e and len(e) >= 2}
    history_repeats = []
    if hist_name_set and not is_exchange:
        seen_rep = set()
        for m in matches:
            for names in (m.get("team1_names"), m.get("team2_names")):
                if not names or len(names) < 2:
                    continue
                k = pair_key(str(names[0]), str(names[1]))
                if k in hist_name_set:
                    history_repeats.append(k)
                    if k not in seen_rep:
                        seen_rep.add(k)
                        issues.append({
                            "severity": "low",
                            "code": "history_pair_repeat",
                            "msg": f"지난 대진표와 겹친 페어: {k[0]}+{k[1]}",
                        })
    scores["history_pairs_available"] = len(hist_name_set)
    scores["history_repeat_pairs"] = len(history_repeats)

    # 교류전 검증 (클럽이 2개 이상일 때만)
    scores["is_exchange"] = is_exchange
    scores["clubs"] = sorted(distinct_clubs)
    cross_club_pairs = 0
    same_club_matches = 0
    if is_exchange:
        for m in matches:
            for team_ids in (m["team1"], m["team2"]):
                ca = players_by_id[team_ids[0]].get("club", "")
                cb = players_by_id[team_ids[1]].get("club", "")
                if ca != cb:
                    cross_club_pairs += 1
                    issues.append({
                        "severity": "high",
                        "code": "cross_club_pair",
                        "msg": f"{min_to_hhmm(m['slot_start'])} {m['court']}코트: 한 팀에 다른 클럽({ca}/{cb}) 혼합 — 교류전 규칙 위반",
                    })
            c1 = players_by_id[m["team1"][0]].get("club", "")
            c2 = players_by_id[m["team2"][0]].get("club", "")
            if c1 == c2:
                same_club_matches += 1
                issues.append({
                    "severity": "low",
                    "code": "same_club_match",
                    "msg": f"{min_to_hhmm(m['slot_start'])} {m['court']}코트: 같은 클럽({c1})끼리 경기 — 인원 사정상 불가피할 수 있음",
                })
    scores["cross_club_pairs"] = cross_club_pairs
    scores["same_club_matches"] = same_club_matches

    three_consec, two_consec = 0, 0
    three_consec_per_player = defaultdict(int)
    for s in player_stats:
        slots = sorted(s["slots_played"])
        for i in range(len(slots)):
            if i >= 2 and slots[i - 1] == slots[i] - 30 and slots[i - 2] == slots[i] - 60:
                three_consec += 1
                three_consec_per_player[s["name"]] += 1
                issues.append({
                    "severity": "high",
                    "code": "three_consec",
                    "msg": f"{s['name']}: 3슬롯 연속 출전 ({min_to_hhmm(slots[i-2])}~{min_to_hhmm(slots[i]+30)}) — 금지 규칙 위반 (알고리즘 버그)",
                })
            elif i >= 1 and slots[i - 1] == slots[i] - 30:
                two_consec += 1
    scores["three_consec"] = three_consec
    scores["two_consec"] = two_consec
    scores["three_consec_max_per_player"] = max(three_consec_per_player.values(), default=0)

    # 경기 사이 긴 공백 (1시간 이상 쉬었다 다시 나오는 경우) + 체류/대기 시간
    long_gaps, long_gap_players, total_idle = 0, 0, 0
    worst_gap = 0
    finish_max = 0
    for s in player_stats:
        slots = sorted(s["slots_played"])
        if not slots:
            continue
        gaps = [slots[i] - slots[i - 1] - 30 for i in range(1, len(slots))]
        mine = [g for g in gaps if g >= THRESHOLDS["long_gap_min"]]
        # 대기 = 실질 도착(eff_in, 코트 개장 시각 반영) 후 첫 경기까지 + 경기 사이 공백
        start_delay = s.get("start_delay")
        if start_delay is None:
            start_delay = max(0, slots[0] - s.get("eff_in", s["in_min"]))
        total_idle += start_delay + sum(g for g in gaps if g > 0)
        finish_max = max(finish_max, slots[-1] + 30)
        if mine:
            long_gaps += len(mine)
            long_gap_players += 1
            worst_gap = max(worst_gap, max(mine))
            for i in range(1, len(slots)):
                g = slots[i] - slots[i - 1] - 30
                if g >= THRESHOLDS["long_gap_min"]:
                    issues.append({
                        "severity": "medium" if g <= 60 else "high",
                        "code": "long_rest_gap",
                        "msg": f"{s['name']}: {min_to_hhmm(slots[i-1]+30)}~{min_to_hhmm(slots[i])} "
                               f"{g}분 공백 (1시간 이상 쉬었다 재출전)",
                    })
    scores["long_gaps"] = long_gaps
    scores["long_gap_players"] = long_gap_players
    scores["long_gap_worst"] = worst_gap
    scores["long_gap_rate"] = round(long_gap_players / len(player_stats), 4) if player_stats else 0
    scores["total_idle_min"] = total_idle
    scores["last_match_end"] = min_to_hhmm(finish_max) if finish_max else "-"

    exps = [p["exp"] for p in parsed["players"]]
    if len(exps) > 1:
        mean_exp = sum(exps) / len(exps)
        var = sum((e - mean_exp) ** 2 for e in exps) / len(exps)
        stdev_exp = var ** 0.5
        outliers = {p["id"] for p in parsed["players"]
                    if stdev_exp > 0 and abs(p["exp"] - mean_exp) > 2 * stdev_exp}
    else:
        outliers = set()

    skill_diffs = []
    skill_diffs_normal = []
    for m in matches:
        d = abs(m["team1_exp_sum"] - m["team2_exp_sum"])
        skill_diffs.append(d)
        has_outlier = any(p_id in outliers for p_id in m["team1"] + m["team2"])
        if not has_outlier:
            skill_diffs_normal.append(d)
        if d > THRESHOLDS["team_skill_max"]:
            t1_names = "+".join(m["team1_names"])
            t2_names = "+".join(m["team2_names"])
            sev = "low" if has_outlier else "medium"
            note = " [구력 outlier 포함]" if has_outlier else ""
            issues.append({
                "severity": sev,
                "code": "skill_diff_large",
                "msg": f"{min_to_hhmm(m['slot_start'])} {m['court']}코트: {t1_names}({m['team1_exp_sum']}) vs {t2_names}({m['team2_exp_sum']}) — 구력차 {d}{note}",
            })
    scores["team_skill_avg"] = round(sum(skill_diffs) / len(skill_diffs), 2) if skill_diffs else 0
    scores["team_skill_max"] = max(skill_diffs) if skill_diffs else 0
    scores["team_skill_max_normal"] = max(skill_diffs_normal) if skill_diffs_normal else 0
    scores["outliers"] = sorted(outliers)

    mixed_violations = 0
    for m in matches:
        if m["type"] != "X":
            continue
        for team_ids, team_names in [(m["team1"], m["team1_names"]), (m["team2"], m["team2_names"])]:
            p_a = players_by_id[team_ids[0]]
            p_b = players_by_id[team_ids[1]]
            male = p_a if p_a["gender"] == "M" else p_b
            female = p_b if p_a["gender"] == "M" else p_a
            if male["exp"] < female["exp"]:
                mixed_violations += 1
                issues.append({
                    "severity": "high",
                    "code": "mixed_skill_rule",
                    "msg": f"{min_to_hhmm(m['slot_start'])} {m['court']}코트 혼복: 남자({male['name']}, {male['exp']}년) 구력이 여자({female['name']}, {female['exp']}년)보다 낮음",
                })
    scores["mixed_skill_violations"] = mixed_violations

    total_matches = len(matches)
    mixed_count = sum(1 for m in matches if m["type"] == "X")
    scores["mixed_ratio"] = round(mixed_count / total_matches, 4) if total_matches else 0
    scores["match_count"] = total_matches
    scores["type_count"] = bracket.get("type_count", {})

    # 부부 페어: '피함' 부부 혼복 같은팀 / 종료시각 30분 초과 차이 (둘 다 소프트 — 정보용)
    # 종료시간차=30 부부(신혁재·방미라)는 반대로 '정확히 30분 차이' 미달성을 보고한다.
    couples = parsed.get("couples") or []
    name_to_id = {p["name"]: p["id"] for p in parsed["players"]}
    stats_by_id = {s["id"]: s for s in player_stats}
    couple_avoid_paired, couple_want_paired, couple_finish_over = [], [], []
    couple_gap30_missed = []
    team_pairs = set()
    for m in matches:
        if m["type"] != "X":
            continue
        for team in (m["team1"], m["team2"]):
            team_pairs.add(pair_key(team[0], team[1]))
    for entry in couples:
        if not entry or len(entry) < 2:
            continue
        a, b = str(entry[0]), str(entry[1])
        want = bool(entry[2]) if len(entry) > 2 else False
        ida, idb = name_to_id.get(a), name_to_id.get(b)
        if not (ida and idb):
            continue
        if pair_key(ida, idb) in team_pairs:
            (couple_want_paired if want else couple_avoid_paired).append(f"{a}+{b}")
            if not want:
                issues.append({
                    "severity": "medium",
                    "code": "couple_pair_avoid_failed",
                    "msg": f"부부 페어 회피 실패: {a}+{b} 혼복 같은 팀 (설정: 피함 — 인원 사정상 불가피할 수 있음)",
                })
        gap_target = None
        if len(entry) > 3 and entry[3] not in (None, "", False):
            try:
                gap_target = int(entry[3])
            except (TypeError, ValueError):
                gap_target = None
        sa = sorted(stats_by_id[ida]["slots_played"])
        sb = sorted(stats_by_id[idb]["slots_played"])
        if sa and sb:
            diff = abs(sa[-1] - sb[-1])
            if gap_target:
                if diff != gap_target:
                    couple_gap30_missed.append(f"{a}+{b}({diff}분)")
                    issues.append({
                        "severity": "medium",
                        "code": "couple_finish_gap_exact",
                        "msg": f"부부 종료 {gap_target}분 차이 미달성: {a} {min_to_hhmm(sa[-1]+30)} / {b} {min_to_hhmm(sb[-1]+30)} 종료 — {diff}분 차이 (목표 정확히 {gap_target}분)",
                    })
            elif diff > 30:
                couple_finish_over.append(f"{a}+{b}({diff}분)")
                issues.append({
                    "severity": "low",
                    "code": "couple_finish_gap",
                    "msg": f"부부 종료시각 차이: {a} {min_to_hhmm(sa[-1]+30)} / {b} {min_to_hhmm(sb[-1]+30)} 종료 — {diff}분 차이 (목표 30분 이내)",
                })
    scores["couple_avoid_paired"] = couple_avoid_paired
    scores["couple_want_paired"] = couple_want_paired
    scores["couple_finish_over30"] = couple_finish_over
    scores["couple_gap30_missed"] = couple_gap30_missed

    # 남자 게스트 남복 우선 — 혼복에 들어간 남자 게스트 자리 수 (정보용, 소프트 선호).
    # 여자 게스트는 혼복 가능이라 세지 않는다.
    # 본인이 '혼복희망'을 적었으면 그 판수까지는 의도된 배정이므로 세지 않는다
    # (안 빼면 희망을 정확히 채운 것이 '남복 우선 실패'로 보고된다).
    guest_seats = defaultdict(int)
    for m in matches:
        if m["type"] != "X":
            continue
        for pid in m["team1"] + m["team2"]:
            p = players_by_id[pid]
            if p.get("membership") == "게스트" and p.get("gender") == "M":
                guest_seats[pid] += 1
    male_guest_mixed_seats = sum(
        max(0, n - int(players_by_id[pid].get("mixed_wish") or 0))
        for pid, n in guest_seats.items())
    scores["male_guest_mixed_seats"] = male_guest_mixed_seats

    males_total = sum(1 for p in parsed["players"] if p["gender"] == "M")
    females_total = sum(1 for p in parsed["players"] if p["gender"] == "F")
    gender_balanced = males_total >= 4 and females_total >= 4

    verdict = "PASS"
    if scores["max_games_violations"]:
        verdict = "RETRY"
    if scores["min_games_violations"]:
        verdict = "RETRY"

    if scores["cross_club_pairs"] > 0:
        verdict = "RETRY"

    if is_exchange:
        if scores["game_gap_within_club"] > THRESHOLDS["game_gap_global"]:
            verdict = "RETRY"
            issues.append({"severity": "high", "code": "game_gap_within_club",
                           "msg": f"클럽 내 게임수 격차 {scores['game_gap_within_club']} > {THRESHOLDS['game_gap_global']} (클럽 간 차이는 정상)"})
    else:
        if scores["game_gap_global"] > THRESHOLDS["game_gap_global"]:
            verdict = "RETRY"
            issues.append({"severity": "high", "code": "game_gap_global",
                           "msg": f"전체 게임수 격차 {scores['game_gap_global']} > {THRESHOLDS['game_gap_global']}"})
    if scores["max_group_gap"] > THRESHOLDS["game_gap_group"]:
        verdict = "RETRY"
    # 교류전에선 페어 중복(여자 인원 적어 같은 페어 불가피)·구력차(여복을 구력 균형보다 우선)는
    # RETRY 사유에서 제외하고 정보로만 표시.
    if not is_exchange and scores["pair_dup_rate"] > THRESHOLDS["pair_dup_rate"]:
        verdict = "RETRY"
    if scores["three_consec_max_per_player"] > THRESHOLDS["three_consec_per_player"]:
        verdict = "RETRY"
    # 코트 대비 인원이 많으면 공백은 구조적으로 불가피 — 참가자 대비 비율로만 판정한다.
    if scores["long_gap_rate"] > THRESHOLDS["long_gap_rate"]:
        verdict = "RETRY"
        issues.append({"severity": "high", "code": "long_gap_rate",
                       "msg": f"1시간 이상 공백을 겪는 인원 비율 {scores['long_gap_rate']*100:.0f}% "
                              f"> {THRESHOLDS['long_gap_rate']*100:.0f}%"})
    if not is_exchange and scores["team_skill_avg"] > THRESHOLDS["team_skill_avg"]:
        verdict = "RETRY"
    if not is_exchange and scores["team_skill_max_normal"] > THRESHOLDS["team_skill_max"]:
        verdict = "RETRY"
    max_male_exp = max((p["exp"] for p in parsed["players"] if p["gender"] == "M"), default=0)
    max_female_exp = max((p["exp"] for p in parsed["players"] if p["gender"] == "F"), default=0)
    structural_mixed_unavoidable = max_female_exp > max_male_exp
    scores["structural_mixed_unavoidable"] = structural_mixed_unavoidable

    if scores["mixed_skill_violations"] > THRESHOLDS["mixed_skill_violations"]:
        if structural_mixed_unavoidable:
            issues.append({
                "severity": "low",
                "code": "mixed_violation_structural",
                "msg": f"여자 최고 구력({max_female_exp}년) > 남자 최고 구력({max_male_exp}년) — 혼복 규칙 위반은 입력 구조적 한계, 알고리즘이 회피 불가",
            })
        elif is_exchange:
            # 교류전: 혼복은 여복 페어 반복을 줄이기 위한 보조 수단 — 구력 매칭이 항상 맞지는 않으므로
            # RETRY 사유에서 제외하고 정보로만 표시.
            issues.append({
                "severity": "low",
                "code": "mixed_violation_exchange",
                "msg": f"교류전 혼복에서 남자 구력 < 여자 구력 {scores['mixed_skill_violations']}건 — 인원 사정상 일부 불가피",
            })
        else:
            verdict = "RETRY"
    if gender_balanced and scores["mixed_ratio"] > THRESHOLDS["mixed_ratio_balanced"]:
        issues.append({"severity": "low", "code": "mixed_ratio_high",
                       "msg": f"혼복 비율 {scores['mixed_ratio']*100:.0f}% — 단성 복식 우선 고려"})

    return {"verdict": verdict, "scores": scores, "issues": issues}


def print_report(review: dict) -> None:
    s = review["scores"]
    print("=" * 60)
    print(f"VERDICT: {review['verdict']}")
    print("=" * 60)
    print(f"매치 수: {s['match_count']}  (남복 {s['type_count'].get('M',0)} / 여복 {s['type_count'].get('F',0)} / 혼복 {s['type_count'].get('X',0)})")
    print(f"게임수: min={s['games_min']}, max={s['games_max']}, avg={s['games_avg']}, 전체격차={s['game_gap_global']}")
    print(f"가용슬롯 그룹 내 최대 격차: {s['max_group_gap']}")
    print(f"페어 중복: {s['pair_dup_count']}쌍 ({s['pair_dup_rate']*100:.1f}%)")
    print(f"같은 4명 재대결: 편 바꿔 {s.get('quad_repeats', 0)}회 / 상대편까지 그대로 {s.get('matchup_repeats', 0)}회")
    if s.get("history_pairs_available"):
        print(f"지난 대진표 대비 반복 페어: {s.get('history_repeat_pairs', 0)}쌍 "
              f"(비교 대상 {s['history_pairs_available']}쌍)")
    print(f"연속 출전: 2슬롯연속 {s['two_consec']}회, 3슬롯연속 {s['three_consec']}회")
    print(f"1시간 이상 공백: {s['long_gaps']}건 / {s['long_gap_players']}명 "
          f"({s['long_gap_rate']*100:.0f}%), 최장 {s['long_gap_worst']}분")
    print(f"총 대기시간(도착~마지막경기 중 안 뛰는 시간): {s['total_idle_min']}분, 마지막 경기 종료 {s['last_match_end']}")
    print(f"팀 구력차: 평균 {s['team_skill_avg']}, 최대 {s['team_skill_max']}")
    print(f"혼복 비율: {s['mixed_ratio']*100:.1f}%")
    print(f"혼복 규칙 위반: {s['mixed_skill_violations']}건")
    if s.get("min_games_violations"):
        print(f"최소게임수 미달: {', '.join(s['min_games_violations'])}")
    if s.get("mixed_wish_short"):
        print(f"혼복희망 미달: {', '.join(s['mixed_wish_short'])}")
    if s.get("male_guest_mixed_seats"):
        print(f"혼복에 들어간 남자 게스트: {s['male_guest_mixed_seats']}자리 (남자 게스트는 남복 우선 — 인원 사정상 일부 가능)")
    if s.get("couple_avoid_paired"):
        print(f"부부 페어 회피 실패(피함): {', '.join(s['couple_avoid_paired'])}")
    if s.get("couple_want_paired"):
        print(f"부부 페어 성사(원함): {', '.join(s['couple_want_paired'])}")
    if s.get("couple_finish_over30"):
        print(f"부부 종료시각 30분 초과: {', '.join(s['couple_finish_over30'])}")
    if s.get("couple_gap30_missed"):
        print(f"부부 종료 30분 차이 미달성: {', '.join(s['couple_gap30_missed'])}")
    if s.get("is_exchange"):
        print(f"교류전: 클럽 {', '.join(s.get('clubs', []))}  |  클럽혼합 페어(위반) {s.get('cross_club_pairs', 0)}건, 같은클럽끼리 경기 {s.get('same_club_matches', 0)}건")
    print("-" * 60)
    if review["issues"]:
        print(f"이슈 {len(review['issues'])}건:")
        for i in review["issues"][:20]:
            print(f"  [{i['severity']}/{i['code']}] {i['msg']}")
        if len(review["issues"]) > 20:
            print(f"  ... ({len(review['issues']) - 20}건 더)")
    else:
        print("이슈 없음")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", required=True)
    ap.add_argument("--bracket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--history", default="",
                    help="지난주/2주전 페어 히스토리 JSON (history.py 산출물). 겹침 페어 정보 표시용.")
    args = ap.parse_args()

    with open(args.parsed, "r", encoding="utf-8") as f:
        parsed = json.load(f)
    with open(args.bracket, "r", encoding="utf-8") as f:
        bracket = json.load(f)

    hist_pairs = None
    if args.history and os.path.exists(args.history):
        with open(args.history, "r", encoding="utf-8") as f:
            hist_pairs = json.load(f).get("pairs", [])

    review = compute_scores(parsed, bracket, hist_pairs)
    print_report(review)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
