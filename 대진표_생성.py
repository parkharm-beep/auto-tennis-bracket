"""테니스 대진표 자동 생성 통합 실행 스크립트.

채워진 입력 엑셀 → 파싱 → 대진 생성 → 품질 검증 → 결과 엑셀 출력을 한 번에 처리.

사용법:
    # 기본
    python 대진표_생성.py --in 테니스_입력양식.xlsx

    # 출력 파일명/날짜 지정
    python 대진표_생성.py --in 테니스_입력양식.xlsx --out 테니스_대진표_20260530.xlsx --date "26.5.30"

    # 다른 시드로 재생성
    python 대진표_생성.py --in 테니스_입력양식.xlsx --seed 99

    # 빈 입력 템플릿만 생성
    python 대진표_생성.py --create-template

    # 이미지 기반 사전 채움 템플릿 생성
    python 대진표_생성.py --create-template --prefill image
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


BASE = Path(__file__).resolve().parent
SKILLS = BASE / ".claude" / "skills"
SCRIPTS = {
    "build_template": SKILLS / "tennis-input-template" / "scripts" / "build_template.py",
    "parse_input":    SKILLS / "tennis-input-template" / "scripts" / "parse_input.py",
    "schedule":       SKILLS / "tennis-scheduling-algorithm" / "scripts" / "schedule.py",
    "review":         SKILLS / "tennis-scheduling-algorithm" / "scripts" / "review.py",
    "history":        SKILLS / "tennis-scheduling-algorithm" / "scripts" / "history.py",
    "render_bracket": SKILLS / "tennis-excel-output"        / "scripts" / "render_bracket.py",
}

INPUT_DIR   = BASE / "입력"
OUTPUT_DIR  = BASE / "출력"
SAMPLES_DIR = BASE / "샘플"
DEFAULT_INPUT  = INPUT_DIR / "테니스_입력양식.xlsx"
DEFAULT_MEMBERS = INPUT_DIR / "클럽멤버_설정.xlsx"   # 있으면 자동 반영 (부부 페어 등)

WORKSPACE = BASE / "_workspace"
HISTORY_JSON = WORKSPACE / "00_history.json"
PARSED_JSON  = WORKSPACE / "01_parsed.json"
BRACKET_JSON = WORKSPACE / "02_bracket.json"
REVIEW_JSON  = WORKSPACE / "03_review.json"


def _date_suffix(date_str: str) -> str:
    """--date 인자를 YYYYMMDD로 변환. 비어있으면 오늘 날짜.

    지원 형식:
      '26.5.30', '26-5-30', '26/5/30'   → 20260530
      '2026.5.30', '2026-5-30'           → 20260530
      '260530'                            → 20260530
      '20260530'                          → 20260530
    """
    if date_str:
        s = date_str.strip()
        m = re.match(r"^(\d{1,4})[.\-/](\d{1,2})[.\-/](\d{1,2})$", s)
        if m:
            yy, mm, dd = m.groups()
            yy = int(yy)
            if yy < 100:
                yy += 2000
            return f"{yy:04d}{int(mm):02d}{int(dd):02d}"
        digits = re.sub(r"\D", "", s)
        if len(digits) == 8:
            return digits
        if len(digits) == 6:
            return f"20{digits}"
    return date.today().strftime("%Y%m%d")


def _default_output(date_str: str) -> Path:
    return OUTPUT_DIR / f"테니스_대진표_{_date_suffix(date_str)}.xlsx"


def _detect_prev_files(target_suffix: str) -> tuple[Path | None, Path | None]:
    """출력/ 폴더에서 '테니스_대진표_YYYYMMDD.xlsx' 중 이번 대진표(target_suffix)보다
    앞선 날짜의 파일 2개를 최신순으로 반환. (1주전, 2주전).
    """
    files = []
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("테니스_대진표_*.xlsx"):
            m = re.search(r"(\d{8})", f.stem)
            if not m:
                continue
            ds = m.group(1)
            if target_suffix and ds >= target_suffix:   # 이번 결과일/미래 파일 제외
                continue
            files.append((ds, f))
    files.sort(key=lambda x: x[0], reverse=True)
    d1 = files[0][1] if len(files) >= 1 else None
    d2 = files[1][1] if len(files) >= 2 else None
    return d1, d2


def _prompt_prev(target_suffix: str) -> tuple[str | None, str | None]:
    """대화형: 최근 대진표를 자동으로 찾아 보여주고 반영 여부를 묻는다."""
    d1, d2 = _detect_prev_files(target_suffix)
    print()
    if d1 or d2:
        print("최근 대진표 파일을 찾았습니다 (겹치는 페어를 피하는 데 사용):")
        if d1:
            print(f"  · 1주전(우선): {d1.name}")
        if d2:
            print(f"  · 2주전:       {d2.name}")
        ans = input("이 대진표들과 겹치는 페어를 최대한 피할까요? "
                    "(Y=반영 / M=파일 직접선택 / N=안함) [Y]: ").strip().lower()
    else:
        print("출력 폴더에서 이전 대진표를 자동으로 찾지 못했습니다.")
        ans = input("이전 대진표 파일을 직접 지정할까요? (M=직접선택 / N=안함) [N]: ").strip().lower()

    if ans in ("n", "no"):
        return (None, None)
    if ans in ("m", "manual") or (ans and ans not in ("y", "yes")):
        p1 = input("  1주전 대진표 파일 경로 (없으면 Enter): ").strip().strip('"')
        p2 = input("  2주전 대진표 파일 경로 (없으면 Enter): ").strip().strip('"')
        return (p1 or None, p2 or None)
    # 기본(Y/Enter) → 자동 감지한 파일 사용
    return (str(d1) if d1 else None, str(d2) if d2 else None)


def _resolve_prev(args, target_suffix: str) -> tuple[str | None, str | None]:
    """이전 대진표(1주전/2주전) 경로를 결정. 우선순위: 명시 > --no-prev > --auto-prev > 대화형.

    아무 것도 지정하지 않으면 반영하지 않는다(요구사항: 안 넣으면 고려 안 함).
    """
    if args.no_prev:
        return (None, None)
    if args.prev1 or args.prev2:
        return (args.prev1, args.prev2)
    if args.auto_prev:
        d1, d2 = _detect_prev_files(target_suffix)
        if d1:
            print(f"[자동] 1주전 대진표: {d1.name}")
        if d2:
            print(f"[자동] 2주전 대진표: {d2.name}")
        if not (d1 or d2):
            print("[자동] 출력 폴더에서 이전 대진표를 찾지 못했습니다 - 페어 회피 없이 진행합니다.")
        return (str(d1) if d1 else None, str(d2) if d2 else None)
    if args.prompt_prev and sys.stdin.isatty():
        return _prompt_prev(target_suffix)
    return (None, None)


def _members_args(args) -> list:
    """parse_input에 넘길 멤버 설정 인자. 우선순위: --no-members > --members > 입력/ 자동감지 > 내장 기본값."""
    if args.no_members:
        return ["--no-members"]
    if args.members:
        return ["--members", args.members]
    if DEFAULT_MEMBERS.exists():
        return ["--members", str(DEFAULT_MEMBERS)]
    return []   # parse_input이 내장 기본값(부부 4쌍) 사용


def _run(label: str, cmd: list[str]) -> int:
    print(f"\n[{label}] {' '.join(str(c) for c in cmd[-4:])}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, *map(str, cmd)], env=env).returncode


def cmd_create_template(args) -> int:
    INPUT_DIR.mkdir(exist_ok=True)
    out = args.out or str(DEFAULT_INPUT)
    rc = _run("템플릿 생성", [SCRIPTS["build_template"], "--out", out, "--prefill", args.prefill])
    if rc == 0:
        print(f"\n완료. 다음 단계:")
        print(f"  1. '{out}' 파일을 열어 빈 칸(노란색)을 채웁니다.")
        print(f"     안내 시트(맨 앞)에 작성 규칙과 알고리즘 조건이 정리돼 있습니다.")
        print(f"  2. 채운 후 다음 명령으로 대진표를 생성합니다:")
        print(f"     python 대진표_생성.py --date \"26.5.30\"")
        print(f"     (--in 생략 시 '{DEFAULT_INPUT.name}'을 자동으로 사용합니다)")
    return rc


def cmd_check(args) -> int:
    """입력 양식만 검사 (대진표 생성 안 함). 오류·경고와 구성 요약을 보여준다."""
    inp = Path(args.inp) if args.inp else DEFAULT_INPUT
    if not inp.exists():
        print(f"[에러] 입력 파일을 찾을 수 없습니다: {inp}")
        print(f"       먼저 'python 대진표_생성.py --create-template'로 양식을 만드세요.")
        return 1

    WORKSPACE.mkdir(exist_ok=True)
    check_json = WORKSPACE / "00_check_parsed.json"
    print("=" * 60)
    print(f"입력 양식 검사: {inp}")
    print("=" * 60)
    rc = _run("입력 검사", [SCRIPTS["parse_input"], "--in", inp, "--out", check_json, *_members_args(args)])
    if rc != 0:
        print("\n[결과] X 입력 양식에 오류가 있습니다.")
        print("       위의 [에러] 메시지를 보고 해당 칸을 고친 뒤 다시 검사하세요.")
        return rc

    import json
    data = json.loads(check_json.read_text(encoding="utf-8"))
    players = data["players"]
    slots = data["schedule_slots"]
    warnings = data.get("warnings", [])

    males = [p for p in players if p["gender"] == "M"]
    females = [p for p in players if p["gender"] == "F"]
    guests = [p for p in players if p["membership"] == "게스트"]
    clubs = {p.get("club", "") for p in players if p.get("club", "")}

    total_seats = 0
    for sl in slots:
        n_avail = sum(1 for p in players if sl["slot_start"] in p["available_slots"])
        total_seats += min(len(sl["courts"]), n_avail // 4) * 4

    print()
    print(f"  참가자 {len(players)}명 (남 {len(males)} / 여 {len(females)}, 게스트 {len(guests)}명)")
    if len(clubs) > 1:
        print(f"  교류전 모드: 클럽 {len(clubs)}개 ({', '.join(sorted(clubs))})")
    print(f"  코트 {len(data['courts'])}개, 슬롯 {len(slots)}개 - 전체 배정 가능 자리 {total_seats}개")
    avg = total_seats / len(players) if players else 0
    print(f"  1인당 평균 예상 게임수: 약 {avg:.1f}게임")

    min_list = [(p["name"], p["min_games"]) for p in players if p.get("min_games")]
    max_list = [(p["name"], p["max_games"]) for p in players if p.get("max_games") is not None]
    if min_list:
        print(f"  최소게임수 지정 {len(min_list)}명: " + ", ".join(f"{n}={v}" for n, v in min_list))
    if max_list:
        print(f"  최대게임수 지정 {len(max_list)}명: " + ", ".join(f"{n}={v}" for n, v in max_list))
    wish_list = [(p["name"], p["mixed_wish"]) for p in players if p.get("mixed_wish")]
    if wish_list:
        print(f"  혼복희망 {len(wish_list)}명: " + ", ".join(f"{n}={v}게임" for n, v in wish_list))
    streak_labels = {"no2": "금지", "ok3": "허용"}
    streak_list = [(p["name"], streak_labels[p["streak"]])
                   for p in players if p.get("streak") in streak_labels]
    if streak_list:
        print(f"  연속게임 지정 {len(streak_list)}명: " + ", ".join(f"{n}={v}" for n, v in streak_list))

    pins = data.get("pins") or []
    if pins:
        seats = sum(1 for pin in pins for side in ("team1", "team2")
                    for pid in (pin.get(side) or []) if pid)
        by_name = {p["id"]: p["name"] for p in players}
        print(f"  씨드 고정 {seats}자리 (경기 {len(pins)}개) - 나머지 자리만 알고리즘이 채웁니다")
        for pin in sorted(pins, key=lambda x: (x["slot_start"], str(x["court"]))):
            t1 = " ".join(by_name.get(i, "?") for i in (pin.get("team1") or []) if i)
            t2 = " ".join(by_name.get(i, "?") for i in (pin.get("team2") or []) if i)
            hh = f"{pin['slot_start'] // 60:02d}:{pin['slot_start'] % 60:02d}"
            print(f"    - {hh} {pin['court']}코트: {t1 or '(빈칸)'} vs {t2 or '(빈칸)'}")

    print()
    if warnings:
        print(f"[결과] O 형식 오류 없음 - 다만 경고 {len(warnings)}건을 확인하세요 (위 [경고] 참조).")
    else:
        print("[결과] O 입력 양식 이상 없음 - 이대로 대진표를 생성할 수 있습니다.")
    print("       생성: python 대진표_생성.py --date \"26.5.30\"  (또는 대진표_생성.bat)")
    return 0


def cmd_generate(args) -> int:
    inp = Path(args.inp) if args.inp else DEFAULT_INPUT
    if not inp.exists():
        print(f"[에러] 입력 파일을 찾을 수 없습니다: {inp}")
        print(f"       먼저 'python 대진표_생성.py --create-template'로 양식을 만드세요.")
        return 1

    WORKSPACE.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    if BRACKET_JSON.exists() and args.keep_prev:
        prev = BASE / "_workspace_prev"
        prev.mkdir(exist_ok=True)
        for f in WORKSPACE.glob("*.json"):
            f.rename(prev / f.name)

    out = Path(args.out) if args.out else _default_output(args.date)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 이전 대진표(1주전/2주전) 결정 → 겹치는 페어 회피용 히스토리 생성 (우리멤버끼리일 때만 실제 반영)
    prev1, prev2 = _resolve_prev(args, _date_suffix(args.date))
    history_arg: list = []
    if prev1 or prev2:
        hist_cmd = [SCRIPTS["history"]]
        if prev1:
            hist_cmd += ["--prev1", prev1]
        if prev2:
            hist_cmd += ["--prev2", prev2]
        hist_cmd += ["--out", HISTORY_JSON]
        rc = _run("0/4 이전 대진표 분석", hist_cmd)
        if rc == 0 and HISTORY_JSON.exists():
            history_arg = ["--history", HISTORY_JSON]
        else:
            print("[경고] 이전 대진표 히스토리 생성 실패 - 페어 회피 없이 진행합니다.")

    print("=" * 60)
    print(f"입력: {inp}")
    print(f"출력: {out}")
    print(f"시드: {args.seed}  반복: {args.iters}")
    if history_arg:
        print(f"이전대진표: 1주전={prev1 or '-'}  2주전={prev2 or '-'}")
    print("=" * 60)

    rc = _run("1/4 입력 파싱",
              [SCRIPTS["parse_input"], "--in", inp, "--out", PARSED_JSON, *_members_args(args)])
    if rc != 0:
        print("\n[중단] 입력 파싱 실패. 위의 [에러]/[경고] 메시지를 확인하세요.")
        return rc

    rc = _run("2/4 대진 생성",
              [SCRIPTS["schedule"], "--in", PARSED_JSON, "--out", BRACKET_JSON,
               "--seed", str(args.seed), "--iters", str(args.iters),
               "--refine", str(args.refine), "--kicks", str(args.kicks), *history_arg])
    if rc != 0:
        return rc

    rc = _run("3/4 품질 검증",
              [SCRIPTS["review"], "--parsed", PARSED_JSON, "--bracket", BRACKET_JSON,
               "--out", REVIEW_JSON, *history_arg])
    if rc != 0:
        return rc

    rc = _run("4/4 결과 엑셀 출력",
              [SCRIPTS["render_bracket"], "--parsed", PARSED_JSON, "--bracket", BRACKET_JSON,
               "--out", out, "--date", args.date, "--title", args.title])
    if rc != 0:
        return rc

    print("\n" + "=" * 60)
    print(f"[완료] {out}")
    print(f"  검증 결과: {REVIEW_JSON.relative_to(BASE)}")
    print(f"  중간 산출물: _workspace\\01_parsed.json, 02_bracket.json")
    print("=" * 60)
    return 0


def main():
    p = argparse.ArgumentParser(
        description="테니스 대진표 자동 생성 통합 스크립트",
        epilog=(
            "기본 파일 규칙:\n"
            "  입력 양식  → 입력/테니스_입력양식.xlsx\n"
            "  결과 파일  → 출력/테니스_대진표_<YYMMDD>.xlsx (--date 기반, 없으면 오늘)\n"
            "  중간 산출  → _workspace/01_parsed.json, 02_bracket.json, 03_review.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--in", dest="inp",
                   help="채워진 입력 엑셀 경로 (기본: 입력/테니스_입력양식.xlsx)")
    p.add_argument("--out",
                   help="출력 엑셀 경로 (기본: 출력/테니스_대진표_<YYMMDD>.xlsx)")
    p.add_argument("--date", default="",
                   help="대진표 상단 날짜 + 출력 파일명 suffix (예: 26.5.30)")
    p.add_argument("--title", default="우리 테니스 클럽 이번 주 대진표")
    p.add_argument("--seed", type=int, default=7, help="알고리즘 시드")
    p.add_argument("--iters", type=int, default=40, help="시드 반복 횟수(초안 생성)")
    p.add_argument("--refine", type=int, default=6,
                   help="상위 N개 초안을 로컬 개선(공백/대기 줄이기)으로 다듬음")
    p.add_argument("--kicks", type=int, default=40,
                   help="로컬 개선의 무작위 교란 횟수 - 크게 하면 품질↑ 시간↑")
    p.add_argument("--keep-prev", action="store_true",
                   help="기존 _workspace 보존 (_workspace_prev/로 이동)")
    p.add_argument("--prev1", help="1주전 대진표 엑셀 경로 (겹치는 페어 회피, 우선순위 높음)")
    p.add_argument("--prev2", help="2주전 대진표 엑셀 경로 (겹치는 페어 회피)")
    p.add_argument("--auto-prev", action="store_true",
                   help="출력/ 폴더에서 최근 대진표 2개를 자동으로 찾아 페어 회피에 반영")
    p.add_argument("--prompt-prev", action="store_true",
                   help="대화형으로 이전 대진표 반영 여부를 물음(자동 감지 + 확인). .bat 기본 흐름에서 사용")
    p.add_argument("--no-prev", action="store_true",
                   help="이전 대진표 페어 회피를 사용하지 않음(명시적으로 끔)")
    p.add_argument("--check", action="store_true",
                   help="대진표 생성 없이 입력 양식만 검사 (오류·경고·구성 요약)")
    p.add_argument("--members",
                   help="멤버 설정 엑셀 경로 (부부 페어 등). 생략 시 입력/클럽멤버_설정.xlsx 자동 감지, 없으면 내장 기본값")
    p.add_argument("--no-members", action="store_true",
                   help="부부 페어 규칙을 끔")
    p.add_argument("--create-members", action="store_true",
                   help="멤버 설정 파일(멤버·부부 시트)을 입력/클럽멤버_설정.xlsx 로 생성")
    p.add_argument("--create-template", action="store_true",
                   help="대진표 생성 대신 빈 입력 양식만 생성")
    p.add_argument("--prefill", default="", choices=["", "image"],
                   help="--create-template과 함께. image=첨부 이미지 기반 사전 채움")
    args = p.parse_args()

    if args.create_template:
        return cmd_create_template(args)

    if args.create_members:
        INPUT_DIR.mkdir(exist_ok=True)
        out = args.out or str(DEFAULT_MEMBERS)
        return _run("멤버 설정 생성", [SCRIPTS["build_template"], "--out", out, "--member-settings"])

    if args.check:
        return cmd_check(args)

    return cmd_generate(args)


if __name__ == "__main__":
    sys.exit(main())
