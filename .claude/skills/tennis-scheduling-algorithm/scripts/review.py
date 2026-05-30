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
    three_consec_per_player=2,
    team_skill_avg=3.0,
    team_skill_max=7,
    mixed_skill_violations=0,
    mixed_ratio_balanced=0.25,
)


def min_to_hhmm(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def compute_scores(parsed: dict, bracket: dict) -> dict:
    matches = bracket["matches"]
    player_stats = bracket["player_stats"]
    players_by_id = {p["id"]: p for p in parsed["players"]}

    scores = {}
    issues = []

    games = [s["games"] for s in player_stats]
    scores["game_gap_global"] = (max(games) - min(games)) if games else 0
    scores["games_min"] = min(games) if games else 0
    scores["games_max"] = max(games) if games else 0
    scores["games_avg"] = round(sum(games) / len(games), 2) if games else 0

    groups = defaultdict(list)
    for s in player_stats:
        if s.get("max_games") is not None:
            continue
        groups[s["available_slots"]].append(s)
    group_gaps = []
    for n_slots, members in groups.items():
        if len(members) < 2:
            continue
        gs = [m["games"] for m in members]
        gap = max(gs) - min(gs)
        group_gaps.append((n_slots, gap))
        if gap > THRESHOLDS["game_gap_group"]:
            issues.append({
                "severity": "high",
                "code": "game_gap_group",
                "msg": f"가용슬롯 {n_slots}개 그룹 내 게임수 격차 {gap} (임계 {THRESHOLDS['game_gap_group']} 초과)",
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

    three_consec, two_consec = 0, 0
    three_consec_per_player = defaultdict(int)
    for s in player_stats:
        slots = sorted(s["slots_played"])
        for i in range(len(slots)):
            if i >= 2 and slots[i - 1] == slots[i] - 30 and slots[i - 2] == slots[i] - 60:
                three_consec += 1
                three_consec_per_player[s["name"]] += 1
                issues.append({
                    "severity": "medium",
                    "code": "three_consec",
                    "msg": f"{s['name']}: 3슬롯 연속 출전 ({min_to_hhmm(slots[i-2])}~{min_to_hhmm(slots[i]+30)})",
                })
            elif i >= 1 and slots[i - 1] == slots[i] - 30:
                two_consec += 1
    scores["three_consec"] = three_consec
    scores["two_consec"] = two_consec
    scores["three_consec_max_per_player"] = max(three_consec_per_player.values(), default=0)

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

    males_total = sum(1 for p in parsed["players"] if p["gender"] == "M")
    females_total = sum(1 for p in parsed["players"] if p["gender"] == "F")
    gender_balanced = males_total >= 4 and females_total >= 4

    verdict = "PASS"
    if scores["max_games_violations"]:
        verdict = "RETRY"

    if scores["game_gap_global"] > THRESHOLDS["game_gap_global"]:
        verdict = "RETRY"
        issues.append({"severity": "high", "code": "game_gap_global",
                       "msg": f"전체 게임수 격차 {scores['game_gap_global']} > {THRESHOLDS['game_gap_global']}"})
    if scores["max_group_gap"] > THRESHOLDS["game_gap_group"]:
        verdict = "RETRY"
    if scores["pair_dup_rate"] > THRESHOLDS["pair_dup_rate"]:
        verdict = "RETRY"
    if scores["three_consec_max_per_player"] > THRESHOLDS["three_consec_per_player"]:
        verdict = "RETRY"
    if scores["team_skill_avg"] > THRESHOLDS["team_skill_avg"]:
        verdict = "RETRY"
    if scores["team_skill_max_normal"] > THRESHOLDS["team_skill_max"]:
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
    print(f"연속 출전: 2슬롯연속 {s['two_consec']}회, 3슬롯연속 {s['three_consec']}회")
    print(f"팀 구력차: 평균 {s['team_skill_avg']}, 최대 {s['team_skill_max']}")
    print(f"혼복 비율: {s['mixed_ratio']*100:.1f}%")
    print(f"혼복 규칙 위반: {s['mixed_skill_violations']}건")
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
    args = ap.parse_args()

    with open(args.parsed, "r", encoding="utf-8") as f:
        parsed = json.load(f)
    with open(args.bracket, "r", encoding="utf-8") as f:
        bracket = json.load(f)

    review = compute_scores(parsed, bracket)
    print_report(review)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
