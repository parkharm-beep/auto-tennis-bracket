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
    "render_bracket": SKILLS / "tennis-excel-output"        / "scripts" / "render_bracket.py",
}

INPUT_DIR   = BASE / "입력"
OUTPUT_DIR  = BASE / "출력"
SAMPLES_DIR = BASE / "샘플"
DEFAULT_INPUT  = INPUT_DIR / "테니스_입력양식.xlsx"

WORKSPACE = BASE / "_workspace"
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

    print("=" * 60)
    print(f"입력: {inp}")
    print(f"출력: {out}")
    print(f"시드: {args.seed}  반복: {args.iters}")
    print("=" * 60)

    rc = _run("1/4 입력 파싱",
              [SCRIPTS["parse_input"], "--in", inp, "--out", PARSED_JSON])
    if rc != 0:
        print("\n[중단] 입력 파싱 실패. 위의 [에러]/[경고] 메시지를 확인하세요.")
        return rc

    rc = _run("2/4 대진 생성",
              [SCRIPTS["schedule"], "--in", PARSED_JSON, "--out", BRACKET_JSON,
               "--seed", str(args.seed), "--iters", str(args.iters)])
    if rc != 0:
        return rc

    rc = _run("3/4 품질 검증",
              [SCRIPTS["review"], "--parsed", PARSED_JSON, "--bracket", BRACKET_JSON,
               "--out", REVIEW_JSON])
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
    p.add_argument("--iters", type=int, default=250, help="시드 반복 횟수")
    p.add_argument("--keep-prev", action="store_true",
                   help="기존 _workspace 보존 (_workspace_prev/로 이동)")
    p.add_argument("--create-template", action="store_true",
                   help="대진표 생성 대신 빈 입력 양식만 생성")
    p.add_argument("--prefill", default="", choices=["", "image"],
                   help="--create-template과 함께. image=첨부 이미지 기반 사전 채움")
    args = p.parse_args()

    if args.create_template:
        return cmd_create_template(args)

    return cmd_generate(args)


if __name__ == "__main__":
    sys.exit(main())
