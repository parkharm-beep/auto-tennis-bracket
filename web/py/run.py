"""Pyodide 진입점.

브라우저(Web Worker)에서 호출되어 입력 엑셀 bytes를 받아 결과 엑셀 bytes를 반환한다.
기존 4개 모듈(parse_input, schedule, review, render_bracket)을 함수로 import해서 직접 호출.
"""
from __future__ import annotations

import json
from io import BytesIO

from openpyxl import load_workbook

from parse_input import (
    parse_players,
    parse_courts,
    build_schedule_slots,
    attach_available_slots,
    min_to_hhmm,
)
from schedule import run_one_seed
from review import compute_scores
from render_bracket import render
from build_template import build_template


def _parse_bytes(xlsx_bytes: bytes) -> dict:
    wb = load_workbook(BytesIO(xlsx_bytes), data_only=True)
    if "참가자" not in wb.sheetnames:
        raise ValueError("입력 엑셀에 '참가자' 시트가 없습니다.")
    if "코트" not in wb.sheetnames:
        raise ValueError("입력 엑셀에 '코트' 시트가 없습니다.")

    players, perr = parse_players(wb["참가자"])
    courts, cerr = parse_courts(wb["코트"])
    errs = perr + cerr
    if errs:
        raise ValueError("입력 오류:\n  - " + "\n  - ".join(errs))

    if len(players) < 4:
        raise ValueError(f"참가자가 {len(players)}명입니다. 최소 4명 필요.")
    if not courts:
        raise ValueError("사용 가능한 코트가 없습니다.")

    schedule_slots = build_schedule_slots(courts)
    attach_available_slots(players, schedule_slots)

    warnings = []
    males = [p for p in players if p["gender"] == "M"]
    females = [p for p in players if p["gender"] == "F"]
    if len(males) < 4:
        warnings.append(f"남자가 {len(males)}명이라 남자복식 불가. 혼복만 가능.")
    if len(females) < 4:
        warnings.append(f"여자가 {len(females)}명이라 여자복식 불가. 혼복만 가능.")

    for sl in schedule_slots:
        avail = [p for p in players if sl["slot_start"] in p["available_slots"]]
        if len(avail) < 4:
            t = f"{min_to_hhmm(sl['slot_start'])}~{min_to_hhmm(sl['slot_end'])}"
            warnings.append(f"슬롯 {t}: 가용 인원 {len(avail)}명 — 일부 코트 공석 가능")

    for p in players:
        if len(p["available_slots"]) == 0:
            warnings.append(f"'{p['name']}': 가용 슬롯 없음")
        if p["max_games"] is not None and p["max_games"] > len(p["available_slots"]):
            warnings.append(
                f"'{p['name']}': 최대게임수({p['max_games']}) > 가용 슬롯수({len(p['available_slots'])}) — 자연 제한됨"
            )

    return {
        "courts": courts,
        "players": players,
        "schedule_slots": schedule_slots,
        "warnings": warnings,
    }


def _schedule(parsed: dict, seed: int, iters: int, candidates: int = 24) -> dict:
    best_state, best_score = None, float("inf")
    best_seed = seed
    for i in range(iters):
        s = seed + i
        state, score = run_one_seed(parsed["players"], parsed["schedule_slots"], s, candidates)
        if score < best_score:
            best_state, best_score = state, score
            best_seed = s
    if best_state is None:
        raise RuntimeError("대진 생성 실패: 가용 인원 부족")

    player_stats = []
    for p in parsed["players"]:
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

    return {
        "matches": best_state["matches"],
        "player_stats": player_stats,
        "type_count": best_state["type_count"],
        "metadata": {"seed": best_seed, "score": best_score, "iterations": iters},
    }


def generate_bracket(
    xlsx_bytes: bytes,
    date_str: str = "",
    seed: int = 7,
    iters: int = 150,
    title: str = "우리 테니스 클럽 대진표",
) -> dict:
    """입력 엑셀 bytes → {xlsx: bytes, review: dict, summary: dict}.

    브라우저에서 호출 후 xlsx 필드를 Blob으로 만들어 다운로드.
    """
    parsed = _parse_bytes(xlsx_bytes)
    bracket = _schedule(parsed, seed, iters)
    review = compute_scores(parsed, bracket)

    out_buf = BytesIO()
    render(parsed, bracket, out_buf, date_str, title)
    out_buf.seek(0)

    return {
        "xlsx_bytes": out_buf.getvalue(),
        "review": review,
        "summary": {
            "players": len(parsed["players"]),
            "courts": len(parsed["courts"]),
            "slots": len(parsed["schedule_slots"]),
            "matches": len(bracket["matches"]),
            "warnings": parsed["warnings"],
        },
    }


def build_empty_template_bytes(prefill: str = "") -> bytes:
    """빈 입력 양식 엑셀을 bytes로 반환. (prefill="image"면 이미지 기반 사전채움)"""
    buf = BytesIO()
    build_template(buf, prefill=prefill)
    buf.seek(0)
    return buf.getvalue()


def generate_bracket_json_result(xlsx_bytes_bin, date_str="", seed=7, iters=150, title="우리 테니스 클럽 대진표"):
    """Pyodide JS 호출용 wrapper. JS의 Uint8Array를 받아 dict 반환.

    xlsx 결과는 별도 함수로 가져가도록 분리하지 않고, 결과 dict에 bytes 그대로 포함.
    """
    if hasattr(xlsx_bytes_bin, "to_py"):
        xlsx_bytes_bin = xlsx_bytes_bin.to_py()
    if not isinstance(xlsx_bytes_bin, (bytes, bytearray)):
        xlsx_bytes_bin = bytes(xlsx_bytes_bin)
    return generate_bracket(bytes(xlsx_bytes_bin), date_str=date_str, seed=seed, iters=iters, title=title)
