"""대진표 알고리즘 회귀 검사 — 실전 입력 여러 건 × 시드 여러 개를 웹 경로 그대로 돌려 기준선과 비교한다.

왜 필요한가:
    규칙을 하나 고치면 다른 규칙이 조용히 무너진다(예: 26.8.14 혼복 하한 조정 → 게임수 균형 회귀,
    사용자가 웹 결과를 직접 대조해 발견). 매번 손으로 A/B를 돌리면 측정 축이 빠진다.
    이 스크립트는 **같은 입력·같은 시드·같은 파라미터**로 모든 지표를 한 번에 비교한다.

무엇을 돌리는가:
    - 코드: `web/py/`(사용자가 실제로 쓰는 웹 경로 `run.generate_bracket`). 시작 전에 미러 3벌
      (`.claude/skills` · `.agents/skills` · `web/py`)이 같은지 먼저 확인한다.
    - 파라미터: 웹 기본값(iters=20, refine=4, kicks=25, max_retries=2). 부부 설정은 내장 기본값.
    - 입력: `회귀/입력/*.xlsx` 전부(파일명 = 케이스 이름). 실명이 들어 있어 커밋하지 않는다(.gitignore).
      케이스를 늘리려면 그 폴더에 입력 엑셀을 복사하고 `--update`로 기준선을 다시 잡는다.

사용법:
    python 회귀/regress.py                 # 기준선과 비교 (지표 악화가 있으면 exit 1)
    python 회귀/regress.py --update        # 현재 결과를 기준선으로 저장
    python 회귀/regress.py --cases 0815_혼복희망,샘플_13명 --seeds 7
    python 회귀/regress.py --jobs 4

판정:
    - 동일    : 모든 시드에서 대진이 기준선과 완전히 같다(리팩터·설정 분리의 증거).
    - 변화    : 대진은 바뀌었지만 '악화' 지표가 없다. 표에서 변화량을 확인한다.
    - 악화    : 아래 FAIL_RULES 중 하나라도 나빠졌다 → exit 1.
    ⚠ '변화'도 사람이 표를 봐야 한다. 공백·대기·혼복 수는 트레이드오프라 자동 판정하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "회귀"
CASE_DIR = HERE / "입력"
BASELINE = HERE / "baseline.json"
LAST_RUN = HERE / "last_run.json"
WEB_PY = ROOT / "web" / "py"

PARAMS = dict(iters=20, refine=4, kicks=25, max_retries=2)
# ⚠ 시드는 멀리 띄운다. solve는 초안을 seed+0..iters-1로, 재시도는 seed+101k로 뽑으므로
#   7과 1처럼 가까우면 초안 20개 중 14개를 공유해 사실상 같은 표본이 된다(실측: 9건 중 4건 해시 동일).
#   7은 웹 기본 시드라 유지한다.
DEFAULT_SEEDS = [7, 5003, 9011]

# 미러 3벌: .claude(CLI 정본) · .agents(Codex) · web/py(웹). 어긋나면 웹과 CLI가 다르게 동작한다.
MIRRORS = {
    "schedule.py":       "tennis-scheduling-algorithm",
    "review.py":         "tennis-scheduling-algorithm",
    "history.py":        "tennis-scheduling-algorithm",
    "parse_input.py":    "tennis-input-template",
    "build_template.py": "tennis-input-template",
    "club_config.py":    "tennis-input-template",
    "render_bracket.py": "tennis-excel-output",
}
# 웹에 안 가는 CLI 전용 파일 — .claude·.agents 두 벌만 비교
MIRRORS_CLI_ONLY = {"build_draft.py": "tennis-input-template"}

# 나빠지면 FAIL — (지표, 방향). 방향 +1 = 커지면 악화, -1 = 작아지면 악화.
# 사용자가 '어기지 않는 규칙'·'최우선'으로 확정한 것만 넣는다.
FAIL_RULES = [
    ("three_consec", +1),          # 3연속 하드 금지 (26.8.7)
    ("two_consec_banned", +1),     # 개인 '연속게임=금지' (26.8.20)
    ("min_games_viol", +1),        # 최소게임수 보장 (26.8.1)
    ("max_games_viol", +1),
    ("max_group_gap", +1),         # 같은 cap끼리 최대 1게임 차 (26.9.3·9.10)
    ("seed_lost", +1),             # 씨드 자리 유실 (26.8.20)
    ("cross_club_pairs", +1),      # 교류전 같은 팀=같은 클럽
    ("couple_avoid", +1),          # '피함' 부부 같은 팀
    ("retryable_high", +1),        # 시드로 고칠 수 있는 high 이슈가 최종안에 남음
]

# 표에 보여 줄 지표 (FAIL 아님 — 트레이드오프라 사람이 판단)
SHOW = [
    ("matches", "경기"), ("F", "여복"), ("X", "혼복"),
    ("long_gaps", "1h공백"), ("long_gap_players", "공백인원"), ("total_idle_min", "총대기"),
    ("pair_dup", "짝중복"), ("quad_repeats", "같은4명"), ("matchup_repeats", "편까지같은재대결"),
    ("skill_avg", "구력차"), ("mixed_skill_viol", "혼복구력위반"),
    ("mixed_wish_short", "혼복희망미달"), ("couple_over30", "부부종료30+"),
    ("male_guest_mixed", "남게스트혼복"), ("attempts", "시도"),
]


# ─────────────────────────── 실행 (워커 프로세스) ───────────────────────────

def _bracket_hash(bracket: dict) -> str:
    rows = []
    for m in bracket.get("matches", []):
        t1 = sorted(m.get("team1") or [])
        t2 = sorted(m.get("team2") or [])
        rows.append((m.get("slot_start"), str(m.get("court")), m.get("type"), sorted([t1, t2])))
    rows.sort(key=lambda r: (r[0], r[1]))
    return hashlib.md5(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _run_one(case: str, path: str, seed: int) -> dict:
    sys.path.insert(0, str(WEB_PY))
    import run  # noqa: E402  (워커마다 web/py를 경로에 올린 뒤 import)

    # 대진 자체(해시)는 generate_bracket이 돌려주지 않으므로 solve를 가로채 마지막 채택안을 잡는다.
    captured = {}
    orig_solve = run.solve

    def spy(*a, **kw):
        b = orig_solve(*a, **kw)
        captured[kw.get("seed")] = b
        return b

    run.solve = spy
    t0 = time.time()
    try:
        res = run.generate_bracket(Path(path).read_bytes(), seed=seed, **PARAMS)
    finally:
        run.solve = orig_solve
    sec = time.time() - t0

    rv, sm = res["review"], res["summary"]
    s = rv["scores"]
    bracket = captured[sm["seed_used"]]
    games = [ps.get("games", 0) for ps in bracket.get("player_stats", [])]
    dist: dict = {}
    for g in games:
        dist[str(g)] = dist.get(str(g), 0) + 1
    tc = s.get("type_count", {})
    high = sorted(i["code"] for i in rv.get("issues", []) if i.get("severity") == "high")
    return {
        "case": case, "seed": seed, "sec": round(sec, 1),
        "hash": _bracket_hash(bracket),
        "verdict": rv.get("verdict"),
        "high": high,
        "retryable_high": len(run._retryable_issue_codes(rv)),
        "attempts": sm.get("attempts", 1),
        "matches": s.get("match_count", 0),
        "M": tc.get("M", 0), "F": tc.get("F", 0), "X": tc.get("X", 0),
        "games_dist": dict(sorted(dist.items(), key=lambda kv: int(kv[0]))),
        "max_group_gap": s.get("max_group_gap", 0),
        "three_consec": s.get("three_consec", 0),
        "two_consec_banned": s.get("two_consec_banned", 0),
        "min_games_viol": len(s.get("min_games_violations", [])),
        "max_games_viol": len(s.get("max_games_violations", [])),
        "seed_lost": s.get("seed_seats", 0) - s.get("seed_seats_kept", 0),
        "cross_club_pairs": s.get("cross_club_pairs", 0),
        "couple_avoid": len(s.get("couple_avoid_paired", [])),
        "couple_over30": len(s.get("couple_finish_over30", [])) + len(s.get("couple_gap30_missed", [])),
        "mixed_wish_short": len(s.get("mixed_wish_short", [])),
        "male_guest_mixed": s.get("male_guest_mixed_seats", 0),
        "long_gaps": s.get("long_gaps", 0),
        "long_gap_players": s.get("long_gap_players", 0),
        "total_idle_min": s.get("total_idle_min", 0),
        "pair_dup": s.get("pair_dup_count", 0),
        "quad_repeats": s.get("quad_repeats", 0),        # 같은 4명 재대결(편 무관)
        "matchup_repeats": s.get("matchup_repeats", 0),  # 그중 편 구성까지 같은 것
        "skill_avg": float(s.get("team_skill_avg", 0)),
        "mixed_skill_viol": s.get("mixed_skill_violations", 0),
    }


# ─────────────────────────── 오케스트레이션 ───────────────────────────

def check_mirrors() -> list[str]:
    bad = []
    for fname, skill in MIRRORS.items():
        paths = [ROOT / ".claude" / "skills" / skill / "scripts" / fname,
                 ROOT / ".agents" / "skills" / skill / "scripts" / fname,
                 WEB_PY / fname]
        digests = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in paths if p.exists()}
        if len(set(digests.values())) > 1 or len(digests) < len(paths):
            bad.append(fname)
    for fname, skill in MIRRORS_CLI_ONLY.items():
        pair = [ROOT / d / "skills" / skill / "scripts" / fname for d in (".claude", ".agents")]
        if len({hashlib.md5(p.read_bytes()).hexdigest() for p in pair if p.exists()}) != 1:
            bad.append(fname)
    return bad


def check_worker_files() -> list[str]:
    """web/py의 파일이 전부 worker.js PY_FILES에 있는가. 여기 빠지면 이 스크립트(디스크에서 import)는
    통과하는데 웹(Pyodide)은 모듈을 못 찾아 통째로 멈춘다."""
    js = (ROOT / "web" / "worker.js").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([\w.]+\.(?:py|json))"', js))
    have = {p.name for p in WEB_PY.iterdir() if p.suffix in (".py", ".json")}
    return sorted(have - listed)


def run_all(cases: list[Path], seeds: list[int], jobs: int) -> list[dict]:
    tasks = [(c.stem, str(c), sd) for c in cases for sd in seeds]
    out = []
    env_note = os.environ.get("PYTHONHASHSEED")
    print(f"실행: 케이스 {len(cases)}건 × 시드 {seeds} = {len(tasks)}회, 병렬 {jobs}"
          f" (PYTHONHASHSEED={env_note or '무작위'})", flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_run_one, *t): t for t in tasks}
        for i, f in enumerate(as_completed(futs), 1):
            case, _, sd = futs[f]
            try:
                r = f.result()
            except Exception as e:  # 한 케이스 실패가 전체를 멈추지 않게
                r = {"case": case, "seed": sd, "error": f"{type(e).__name__}: {e}"}
            out.append(r)
            tag = r.get("error") or f"{r['verdict']} {r['sec']}s"
            print(f"  [{i}/{len(tasks)}] {case} 시드{sd}: {tag}", flush=True)
    print(f"완료: {time.time() - t0:.0f}초", flush=True)
    out.sort(key=lambda r: (r["case"], r["seed"]))
    return out


AVG_KEYS = {"skill_avg"}


def _agg(rows: list[dict], key: str):
    vals = [r.get(key) for r in rows]
    if any(v is None for v in vals):
        return None  # 기준선에 없는 지표(새로 추가됨) — --update 필요
    if key in AVG_KEYS:
        return round(sum(vals) / len(vals), 2)
    return sum(vals)


def _worse(crows, brows) -> list[str]:
    out = []
    for key, sign in FAIL_RULES:
        for c, b in zip(crows, brows):
            if key not in b:
                continue  # 기준선에 없는 지표는 판정 불가 — 표에서 '기준선 없음'으로 보인다
            if (c[key] - b[key]) * sign > 0:
                out.append(f"{key} {b[key]}→{c[key]} (시드{c['seed']})")
    return out


def compare(base: dict, cur: list[dict]) -> int:
    bidx = {(r["case"], r["seed"]): r for r in base["results"]}
    cases = sorted({r["case"] for r in cur})
    fails = []
    count = {"동일": 0, "지표만 변화": 0, "변화": 0, "악화": 0, "오류": 0, "기준선 없음": 0}
    print()
    for case in cases:
        crows = [r for r in cur if r["case"] == case]
        brows = [bidx.get((case, r["seed"])) for r in crows]
        errs = [r for r in crows if "error" in r]
        if errs:
            count["오류"] += 1
            fails.append(f"{case}: 실행 오류 — {errs[0]['error']}")
            print(f"■ {case}: 오류 — {errs[0]['error']}")
            continue
        if any(b is None for b in brows):
            count["기준선 없음"] += 1
            print(f"■ {case}: 기준선 없음 (--cases {case} --update로 추가)")
            continue
        same_bracket = all(c["hash"] == b["hash"] for c, b in zip(crows, brows))
        worse = _worse(crows, brows)
        # 대진이 같아도 지표는 비교한다 — review.py만 바뀌면 대진은 그대로인데 판정이 달라진다
        # (예: 3연속 검출이 깨져 늘 0을 돌려주는 회귀).
        metric_diff = [
            f"시드{c['seed']} {k}: {b.get(k)}→{c.get(k)}"
            for c, b in zip(crows, brows)
            for k in [x for x, _ in FAIL_RULES] + ["verdict", "high", "games_dist"]
            if c.get(k) != b.get(k)
        ]
        if worse:
            state = "악화"
        elif same_bracket:
            state = "지표만 변화" if metric_diff else "동일"
        else:
            state = "변화"
        count[state] += 1
        if state == "동일":
            print(f"■ {case}: 동일 (시드 {len(crows)}개 대진·지표 완전 일치)")
            continue
        print(f"■ {case}: {state}" + (" (대진은 같고 review 판정이 달라짐 — review.py 변경 확인)"
                                     if state == "지표만 변화" else ""))
        if not same_bracket:
            cells = []
            for key, label in SHOW:
                bv, cv = _agg(brows, key), _agg(crows, key)
                if bv is None:
                    cells.append(f"{label} (기준선 없음)→{cv}")
                elif bv == cv:
                    cells.append(f"{label} {cv}")
                else:
                    cells.append(f"{label} {bv}→{cv} ({'+' if cv > bv else ''}{round(cv - bv, 2)})")
            print("   합계(시드 합, 구력차는 평균): " + " · ".join(cells))
        for d in metric_diff:
            print(f"   · {d}")
        for w in worse:
            print(f"   ✗ {w}")
        if worse:
            fails.append(f"{case}: " + "; ".join(worse))
    print()
    print(f"요약: 케이스 {len(cases)}건 — " + " · ".join(f"{k} {v}" for k, v in count.items() if v or k == "동일"))
    for f in fails:
        print(f"  ✗ {f}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="현재 결과를 기준선으로 저장")
    ap.add_argument("--cases", default="", help="쉼표로 구분한 케이스 이름(기본: 전부)")
    ap.add_argument("--seeds", default=",".join(map(str, DEFAULT_SEEDS)))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()

    bad = check_mirrors()
    if bad:
        print(f"✗ 미러 불일치: {', '.join(bad)} — .claude/skills 정본을 .agents/skills·web/py로 복사한 뒤 다시 실행")
        return 2
    missing = check_worker_files()
    if missing:
        print(f"✗ web/worker.js PY_FILES에 없는 파일: {', '.join(missing)} — 웹에서 import가 실패한다")
        return 2

    cases = sorted(CASE_DIR.glob("*.xlsx"))
    if args.cases:
        want = set(args.cases.split(","))
        cases = [c for c in cases if c.stem in want]
    if not cases:
        print(f"✗ 케이스 없음: {CASE_DIR}")
        return 2
    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]

    cur = run_all(cases, seeds, args.jobs)
    LAST_RUN.write_text(json.dumps({"params": PARAMS, "results": cur}, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    if args.update:
        if any("error" in r for r in cur):
            print("✗ 오류가 난 케이스가 있어 기준선을 저장하지 않았습니다.")
            return 1
        keep = []
        if BASELINE.exists():  # 일부 케이스만 갱신할 때 나머지는 보존
            old = json.loads(BASELINE.read_text(encoding="utf-8"))
            done = {(r["case"], r["seed"]) for r in cur}
            keep = [r for r in old["results"] if (r["case"], r["seed"]) not in done]
        data = {"params": PARAMS, "results": sorted(keep + cur, key=lambda r: (r["case"], r["seed"]))}
        BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"기준선 저장: {BASELINE.relative_to(ROOT)} ({len(data['results'])}건)")
        return 0

    if not BASELINE.exists():
        print("✗ 기준선이 없습니다. 먼저 --update로 만드십시오.")
        return 2
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    if base.get("params") != PARAMS:
        print(f"✗ 기준선 파라미터 {base.get('params')} ≠ 현재 {PARAMS} — --update로 다시 잡으십시오.")
        return 2
    return compare(base, cur)


if __name__ == "__main__":
    sys.exit(main())
