# 테니스 대진표 — 웹 PoC

브라우저에서 입력 엑셀을 올리면 Pyodide(브라우저 안의 Python)가 대진표를 만들어 다운로드해줍니다. **서버 없음 / 데이터 외부 전송 없음**.

배포 URL: https://parkharm-beep.github.io/auto-tennis-bracket/

## 로컬 테스트

```powershell
cd C:\Works\auto-tennis-bracket\web
python -m http.server 8000
```

→ 브라우저에서 http://localhost:8000

(StatiCrypt 적용 전 로컬 테스트 — 비번 입력 화면이 안 나옴. 배포본만 비번 게이트가 적용됩니다.)

## GitHub Pages 배포 (최초 1회 셋업)

1. **리포지토리 준비** — 이 폴더 전체를 `parkharm-beep/auto-tennis-bracket` 리포에 push (main 브랜치)
2. **클럽 비번 등록** — 리포 페이지에서:
   - Settings → Secrets and variables → Actions → New repository secret
   - Name: `SITE_PASSWORD`
   - Value: 클럽 공유 비밀번호 (8자 이상 권장)
3. **Actions 자동 실행** — main에 push되면 `.github/workflows/deploy.yml`이:
   - `web/` 폴더를 빌드 디렉토리로 복사
   - StatiCrypt로 `index.html`을 비번 암호화
   - `gh-pages` 브랜치로 배포
4. **Pages 활성화 (최초 1회만)** — Settings → Pages →
   - Source: `Deploy from a branch`
   - Branch: `gh-pages` / `/ (root)` → Save
5. **확인** — 1~2분 뒤 https://parkharm-beep.github.io/auto-tennis-bracket/ 접속 → 비번 입력 화면 → 통과하면 대진표 페이지

## 비번 변경

Secrets의 `SITE_PASSWORD` 값을 수정 → Actions 탭에서 워크플로우 수동 재실행 (Re-run all jobs). 또는 아무 커밋이나 push.

## 파일 구조

```
web/
├── index.html       ← 메인 페이지 (StatiCrypt가 암호화)
├── app.js           ← UI ↔ 워커 통신
├── worker.js        ← Pyodide 로드, 알고리즘 호출
└── py/
    ├── parse_input.py    ← 입력 엑셀 파싱
    ├── schedule.py       ← 대진 알고리즘
    ├── review.py         ← 품질 검증
    ├── render_bracket.py ← 결과 엑셀 렌더링
    ├── build_template.py ← 입력 양식 생성
    └── run.py            ← 통합 진입점 (브라우저에서 import)
```

`web/` 폴더를 통째로 정적 호스팅에 올리면 동작합니다.

## 알고리즘 수정 시

- `py/` 안의 파일을 수정 → main에 push → Actions가 자동 재배포
- 로컬 테스트: `python -m http.server 8000`로 즉시 확인
- 같은 알고리즘이 CLI(`대진표_생성.py`)에서도 작동하므로 동기화 필요 시 `.claude/skills/.../scripts/`와 `web/py/`를 함께 수정

## 한계

- **첫 로딩** 약 10MB 다운로드 (Pyodide + openpyxl), 5~10초 — 두 번째부터 캐시
- **모바일** 속도는 떨어짐 (PC 우선 설계)
- **비번 보호**는 진입 차단 수준 — 진짜 민감한 데이터 보호용이 아님
