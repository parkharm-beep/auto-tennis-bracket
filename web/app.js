/* 메인 페이지 ↔ 워커 통신 + UI 갱신. */

const $ = (id) => document.getElementById(id);
const logEl = $("log");
const fileEl = $("file");
const prev1El = $("prev1");
const prev2El = $("prev2");
const prevClearBtn = $("prev-clear");
const dateEl = $("date");
const seedEl = $("seed");
const itersEl = $("iters");
const titleEl = $("title");
const runBtn = $("run");
const tplPrefillBtn = $("tpl-prefill");
const tplMembersBtn = $("tpl-members");
const membersEl = $("members");
const summaryEl = $("summary");

function log(msg) {
  const t = new Date().toTimeString().slice(0, 8);
  logEl.textContent += `[${t}] ${msg}\n`;
  logEl.scrollTop = logEl.scrollHeight;
}

function setBusy(busy, label = "대진표 생성") {
  runBtn.disabled = busy;
  tplPrefillBtn.disabled = busy;
  if (tplMembersBtn) tplMembersBtn.disabled = busy;
  runBtn.textContent = busy ? "처리 중…" : label;
}

function downloadBlob(bytes, filename, mime) {
  const blob = new Blob([bytes], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
    a.remove();
  }, 100);
}

let pendingTemplateName = null;

let workerReady = false;
const WORKER_VERSION = Date.now();
const worker = new Worker(`./worker.js?v=${WORKER_VERSION}`);

worker.onmessage = (e) => {
  const { type, msg, payload } = e.data;
  if (type === "log") {
    log(msg);
  } else if (type === "ready") {
    workerReady = true;
    log("준비 완료. 입력 엑셀을 선택하고 [대진표 생성]을 누르세요.");
    setBusy(false);
  } else if (type === "error") {
    log("⚠ 에러: " + msg);
    setBusy(false);
  } else if (type === "done") {
    handleDone(payload);
  } else if (type === "template_done") {
    const fname = pendingTemplateName || "테니스_입력양식.xlsx";
    pendingTemplateName = null;
    downloadBlob(
      payload.xlsx,
      fname,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    );
    log(`양식 다운로드 완료: ${fname}`);
    setBusy(false);
  } else if (type === "member_settings_done") {
    downloadBlob(
      payload.xlsx,
      "클럽멤버_설정.xlsx",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    );
    log("멤버 설정 파일 다운로드 완료: 클럽멤버_설정.xlsx (부부 시트 수정 후 '멤버 설정' 칸에 올리면 반영)");
    setBusy(false);
  }
};

worker.onerror = (e) => {
  log("⚠ 워커 에러: " + e.message);
  setBusy(false);
};

log("Pyodide 초기화 시작…");
setBusy(true, "초기화 중…");
worker.postMessage({ type: "init" });

function defaultDateStr() {
  // 경기일은 토요일 — 기본값을 돌아오는 토요일로 (오늘이 토요일이면 오늘).
  const d = new Date();
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  const yy = String(d.getFullYear()).slice(2);
  return `${yy}.${d.getMonth() + 1}.${d.getDate()}`;
}
function outputFileName(dateStr) {
  const d = new Date();
  const s = dateStr.match(/^(\d{1,4})[.\-/](\d{1,2})[.\-/](\d{1,2})$/);
  let yyyy, mm, dd;
  if (s) {
    let yy = parseInt(s[1], 10);
    if (yy < 100) yy += 2000;
    yyyy = String(yy);
    mm = String(parseInt(s[2], 10)).padStart(2, "0");
    dd = String(parseInt(s[3], 10)).padStart(2, "0");
  } else {
    yyyy = String(d.getFullYear());
    mm = String(d.getMonth() + 1).padStart(2, "0");
    dd = String(d.getDate()).padStart(2, "0");
  }
  return `테니스_대진표_${yyyy}${mm}${dd}.xlsx`;
}

dateEl.value = defaultDateStr();

tplPrefillBtn.addEventListener("click", () => {
  if (!workerReady) { log("아직 준비 중입니다."); return; }
  pendingTemplateName = "테니스_입력양식_사전채움.xlsx";
  setBusy(true);
  log("사전채움 양식 생성 요청…");
  worker.postMessage({ type: "template", payload: { prefill: "image" } });
});

if (tplMembersBtn) {
  tplMembersBtn.addEventListener("click", () => {
    if (!workerReady) { log("아직 준비 중입니다."); return; }
    setBusy(true);
    log("멤버 설정 파일 생성 요청…");
    worker.postMessage({ type: "member_settings" });
  });
}

if (prevClearBtn) {
  prevClearBtn.addEventListener("click", () => {
    prev1El.value = "";
    prev2El.value = "";
    log("이전 대진표 선택을 지웠습니다.");
  });
}

runBtn.addEventListener("click", async () => {
  if (!workerReady) {
    log("아직 준비 중입니다. 잠시 후 다시 시도하세요.");
    return;
  }
  const f = fileEl.files[0];
  if (!f) {
    alert("입력 엑셀 파일을 선택하세요.");
    return;
  }
  setBusy(true);
  summaryEl.innerHTML = "";
  log(`입력 파일: ${f.name} (${(f.size / 1024).toFixed(1)} KB)`);
  const buf = await f.arrayBuffer();

  const p1f = prev1El && prev1El.files[0];
  const p2f = prev2El && prev2El.files[0];
  const prev1 = p1f ? await p1f.arrayBuffer() : null;
  const prev2 = p2f ? await p2f.arrayBuffer() : null;
  if (p1f) log(`지난주 대진표: ${p1f.name}`);
  if (p2f) log(`2주전 대진표: ${p2f.name}`);

  const mf = membersEl && membersEl.files[0];
  const members = mf ? await mf.arrayBuffer() : null;
  if (mf) log(`멤버 설정: ${mf.name} (부부 페어 반영)`);
  else log("멤버 설정: 내장 기본값 사용 (부부 4쌍)");

  const transfer = [buf];
  if (prev1) transfer.push(prev1);
  if (prev2) transfer.push(prev2);
  if (members) transfer.push(members);

  worker.postMessage(
    {
      type: "generate",
      payload: {
        bytes: buf,
        prev1,
        prev2,
        members,
        dateStr: dateEl.value || defaultDateStr(),
        seed: parseInt(seedEl.value, 10) || 7,
        iters: parseInt(itersEl.value, 10) || 20,
        title: titleEl.value || "우리 테니스 클럽 대진표",
        // 공백·대기 줄이기(로컬 개선) 강도. 브라우저는 네이티브보다 느려 CLI 기본값보다 낮게 잡는다.
        refine: 4,
        kicks: 25,
      },
    },
    transfer
  );
});

function handleDone({ xlsx, review, summary, elapsed }) {
  log(`완료 (${elapsed}초). 결과 파일 다운로드 준비.`);
  const blob = new Blob([xlsx], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const fname = outputFileName(dateEl.value);
  const url = URL.createObjectURL(blob);

  const a = document.createElement("a");
  a.href = url;
  a.download = fname;
  a.textContent = `${fname} 다운로드`;
  a.className = "download-link";

  const v = review.verdict || "?";
  const s = review.scores || {};
  const issues = (review.issues || []).slice(0, 10);

  summaryEl.innerHTML = `
    <div class="result-box">
      <div class="result-header">결과 (${elapsed}초)</div>
      <p><strong>참가자</strong> ${summary.players}명 · <strong>코트</strong> ${summary.courts}개 · <strong>매치</strong> ${summary.matches}개</p>
      <p><strong>판정</strong>: <span class="verdict verdict-${v.toLowerCase()}">${v}</span></p>
      <p><strong>게임수</strong> 평균 ${s.games_avg} (min ${s.games_min} / max ${s.games_max}, 격차 ${s.game_gap_global})</p>
      <p><strong>페어 중복</strong> ${s.pair_dup_count}쌍 · <strong>완전 중복 대진</strong> ${s.matchup_repeats ?? 0}건 · <strong>3연속</strong> ${s.three_consec}건</p>
      <p><strong>복식 구성</strong> 남복 ${(s.type_count || {}).M ?? 0} / 여복 ${(s.type_count || {}).F ?? 0} / 혼복 ${(s.type_count || {}).X ?? 0} (혼복 ${Math.round((s.mixed_ratio || 0) * 100)}%)</p>
      <p><strong>1시간 이상 공백</strong> ${s.long_gaps ?? 0}건 / ${s.long_gap_players ?? 0}명 (최장 ${s.long_gap_worst ?? 0}분) · <strong>마지막 경기 종료</strong> ${s.last_match_end || "-"}</p>
      ${
        summary.history_ignored_exchange
          ? `<p><strong>지난 대진표 페어 회피</strong> 교류전이라 미적용</p>`
          : summary.history_pairs
          ? `<p><strong>지난 대진표 대비 반복 페어</strong> ${summary.history_repeat_pairs}쌍 (비교 대상 ${summary.history_pairs}쌍) — 적을수록 좋음</p>`
          : ""
      }
      ${
        summary.couples_present
          ? `<p><strong>부부 페어</strong> 이번 주 참가 ${summary.couples_present}쌍 (${summary.members_uploaded ? "업로드한 설정" : "기본 설정"}) — 혼복 같은팀: 원함 ${(s.couple_want_paired || []).length ? (s.couple_want_paired || []).join(", ") : "없음"} / 피함인데 발생 ${(s.couple_avoid_paired || []).length ? (s.couple_avoid_paired || []).join(", ") : "0건"} · 종료 30분 초과 ${(s.couple_finish_over30 || []).length}쌍${(s.couple_gap30_missed || []).length ? ` · 종료 30분차이 미달성 ${(s.couple_gap30_missed || []).join(", ")}` : ""}</p>`
          : ""
      }
      ${
        summary.warnings && summary.warnings.length
          ? `<details><summary>경고 ${summary.warnings.length}건</summary><ul>${summary.warnings.map((w) => `<li>${w}</li>`).join("")}</ul></details>`
          : ""
      }
      ${
        issues.length
          ? `<details><summary>이슈 ${review.issues.length}건 (상위 10건)</summary><ul>${issues.map((i) => `<li>[${i.severity}] ${i.msg}</li>`).join("")}</ul></details>`
          : ""
      }
    </div>
  `;
  summaryEl.prepend(a);
  setBusy(false);
}
