"""클럽 사실 로더 — 정본은 `web/py/club_config.json` 한 파일이다.

코트 운영시간·회원 평소 참석·부부·등급 호칭처럼 '클럽에 대한 사실'은 여기서 읽고,
알고리즘 규칙(가중치·하드 규칙)은 schedule.py/review.py에 둔다. 사실이 바뀌면 JSON만 고친다.

찾는 순서: 이 모듈과 같은 폴더(웹 Pyodide — worker.js가 py/ 파일을 한 폴더에 푼다)
→ 상위 폴더들의 `web/py/`(CLI의 .claude/skills·.agents/skills 미러). 사본을 두지 않는다.

아래 상수는 종전 하드코딩 상수와 **같은 이름·같은 모양(튜플)** 으로 만든다 — 기존 호출부 무수정.
"""
from __future__ import annotations

import json
from pathlib import Path

_FILE = "club_config.json"


def config_path() -> Path:
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        for cand in (d / _FILE, d / "web" / "py" / _FILE):
            if cand.is_file():
                return cand
    raise FileNotFoundError(f"{_FILE}을 찾을 수 없습니다 (정본 위치: web/py/{_FILE})")


def load() -> dict:
    return json.loads(config_path().read_text(encoding="utf-8"))


def _blank(v):
    return "" if v is None else v


def _hhmm(v) -> int:
    h, m = str(v).split(":")
    return int(h) * 60 + int(m)


def validate(cfg: dict) -> list[str]:
    """값 모양 검사. 이 파일이 클럽 사실의 편집 지점이라 오타가 조용히 뜻을 바꾸면 안 된다
    (예: "원함": "피함" 은 truthy라 '피함' 부부가 '원함'으로 뒤집힌다). 문제 목록을 돌려준다."""
    errs: list[str] = []

    def t(cond, msg):
        if not cond:
            errs.append(msg)

    for c in cfg["코트"]["목록"]:
        tag = f"코트 {c.get('이름')!r}"
        try:
            t(_hhmm(c["시작"]) < _hhmm(c["종료"]), f"{tag}: 시작이 종료보다 늦거나 같음")
        except Exception:
            errs.append(f"{tag}: 시작·종료는 'HH:MM' 형식이어야 함")
    for k, v in cfg["등급구력"].items():
        if not k.startswith("_"):
            t(isinstance(v, int) and not isinstance(v, bool), f"등급구력 {k!r}: 정수여야 함")
    seen_kakao, seen_name = set(), set()
    for m in cfg["회원"]["목록"]:
        tag = f"회원 {m.get('이름')!r}"
        t(m.get("성별") in ("남", "여"), f"{tag}: 성별은 '남'/'여'")
        t(isinstance(m.get("구력"), int) and not isinstance(m.get("구력"), bool), f"{tag}: 구력은 정수")
        t(isinstance(m.get("채움"), bool), f"{tag}: 채움은 true/false")
        t(m.get("연속게임", "") in ("", "금지", "허용"), f"{tag}: 연속게임은 ''/'금지'/'허용'")
        for f in ("최소게임수", "최대게임수"):
            v = m.get(f)
            t(v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0),
              f"{tag}: {f}는 null 또는 0 이상 정수")
        try:
            t(_hhmm(m["IN"]) < _hhmm(m["OUT"]), f"{tag}: IN이 OUT보다 늦거나 같음")
        except Exception:
            errs.append(f"{tag}: IN·OUT은 'HH:MM' 형식이어야 함")
        t(m.get("이름") not in seen_name, f"{tag}: 이름 중복")
        t(m.get("카톡아이디") not in seen_kakao, f"{tag}: 카톡아이디 {m.get('카톡아이디')!r} 중복")
        seen_name.add(m.get("이름"))
        seen_kakao.add(m.get("카톡아이디"))
    for c in cfg["부부"]["목록"]:
        tag = f"부부 {c.get('이름1')}·{c.get('이름2')}"
        t(isinstance(c.get("원함"), bool), f"{tag}: 원함은 true/false")
        t(c.get("종료시간차") in (None, 30), f"{tag}: 종료시간차는 null 또는 30")
    return errs


_CFG = load()
_ERRS = validate(_CFG)
if _ERRS:
    raise ValueError(f"{_FILE} 값 오류:\n  - " + "\n  - ".join(_ERRS))

# (이름, 시작, 종료)
COURTS_DEFAULT = [(c["이름"], c["시작"], c["종료"]) for c in _CFG["코트"]["목록"]]

# 게스트 등급 호칭 → 구력
RANK_EXP = {k: v for k, v in _CFG["등급구력"].items() if not k.startswith("_")}

_MEMBERS = _CFG["회원"]["목록"]

# 사전채움 행: (이름, 성별, 구력, 구분, IN, OUT, 최소게임수, 최대게임수, 연속게임, 채움, 메모)
PREFILL_FROM_IMAGE = [
    (m["이름"], m["성별"], m["구력"], m.get("구분", "정회원"), m["IN"], m["OUT"],
     _blank(m.get("최소게임수")), _blank(m.get("최대게임수")), m.get("연속게임", ""),
     "예" if m["채움"] else "", m.get("메모", ""))
    for m in _MEMBERS
]

# 멤버 설정 기본값: (카톡아이디, 실제이름, 성별, 구력, 메모) — 멤버 설정의 메모는 비워 둔다
#   (사전채움 메모는 입력 양식용이라 멤버 시트에 옮기지 않는다 — 종전 동작과 같다).
# 행 순서 = 카톡아이디 가나다순, 영문 아이디는 뒤 (종전 멤버 시트 순서와 같다).
MEMBERS_DEFAULT = sorted(
    [(m["카톡아이디"], m["이름"], m["성별"], m["구력"], "") for m in _MEMBERS],
    key=lambda r: (r[0][:1].isascii(), r[0]))

# 부부: (이름1, 이름2, 원함, 종료시간차)
COUPLES_DEFAULT = [(c["이름1"], c["이름2"], c["원함"], c.get("종료시간차"))
                   for c in _CFG["부부"]["목록"]]
