---
name: tennis-input-template
description: 테니스 대진표 입력용 빈 엑셀 템플릿을 생성하고, 채워진 입력 엑셀을 파싱한다. "입력 양식", "입력 템플릿", "참가자 명단 엑셀" 요청 또는 채워진 .xlsx 입력 파일 파싱이 필요할 때 사용.
---

# Tennis Input Template

빈 입력 엑셀 템플릿 생성 + 채워진 입력 엑셀 파싱을 담당.

## 입력 엑셀 구조

두 시트로 구성:

### 시트 1: `참가자`
| 컬럼 | 형식 | 예시 | 비고 |
|------|------|------|------|
| 번호 | 정수 | 1 | 자동 부여, 사용자가 안 적어도 됨 |
| 이름 | 문자열 | 김도윤 | 중복 불가 |
| 성별 | "남" 또는 "여" | 남 | |
| 구력 | 정수 (년) | 5 | 0 이상 |
| 구분 | "정회원" 또는 "게스트" | 정회원 | |
| 클럽 | 문자열 | (옵션) | 교류전 때만 입력, 비우면 '우리클럽' |
| IN시간 | HH:MM | 08:00 | 30분 단위 |
| OUT시간 | HH:MM | 12:00 | 30분 단위, IN보다 늦어야 함 |
| 최소게임수 | 정수 | (옵션) | 적으면 그 이상 배정 보장(가용 슬롯 한도), 1 이상 |
| 최대게임수 | 정수 | (옵션) | 적으면 그 이하로만 배정(hard), 1 이상 |
| 메모 | 문자열 | (옵션) | 자유 기재 |

### 시트 2: `코트`
| 컬럼 | 형식 | 예시 |
|------|------|------|
| 코트명 | 문자열 | A |
| 시작시간 | HH:MM | 08:00 |
| 종료시간 | HH:MM | 12:00 |

기본값으로 A (08:00-12:00), B (08:00-12:00), C (07:00-09:00) 3행을 채워둠. 사용자가 자유롭게 추가/삭제/수정.

### 시트 3: `안내` (읽기 전용, 사용자가 채우지 않음)
- 작성 가이드와 주의사항 텍스트

## 멤버 설정 파일 (클럽멤버_설정.xlsx)

`build_member_settings()`로 생성(CLI `--member-settings` / 웹 '멤버 설정 다운로드'), `parse_member_settings()`로 파싱.
- **멤버 시트**: 번호·카톡아이디·실제이름 (26.8.2 카톡 투표 확정본 25명, `MEMBERS_DEFAULT`)
- **부부 시트**: 이름1·이름2·부부페어(원함/피함 드롭다운)·메모. 기본값 `COUPLES_DEFAULT` 4쌍
  (박경수·서명숙/신혁재·방미라/원유철·이지은 피함, 한병익·전혜선 원함)
- 파싱은 부부 시트만 알고리즘에 전달(`parsed["couples"]`). 이름은 참가자 시트의 실제이름과 일치해야 매칭.
- 우선순위: `--members` 명시 > `입력/클럽멤버_설정.xlsx` 자동 감지 > 내장 기본값 > `--no-members`로 끔

## 사용 방법

### 빈 템플릿 생성
```powershell
python C:\Works\auto-tennis-bracket\.claude\skills\tennis-input-template\scripts\build_template.py --out C:\Works\auto-tennis-bracket\테니스_입력양식.xlsx
```

### 첨부 이미지 기반 사전 채움
```powershell
python C:\Works\auto-tennis-bracket\.claude\skills\tennis-input-template\scripts\build_template.py --out C:\Works\auto-tennis-bracket\테니스_입력양식.xlsx --prefill image
```

`--prefill image` 옵션: **우리클럽 정회원 로스터**(`PREFILL_FROM_IMAGE`, 26.8.1 사용자 제공 입력양식 기준 18명 — 김효순 표기, 개인별 평소 IN/OUT·최대게임수 포함)를 사전 채움. 게스트는 매주 달라지므로 담지 않는다(사용자가 행 추가). 웹의 "사전채움 양식 다운로드" 버튼이 이 로스터를 쓴다(빈 양식 버튼은 26.8.1 제거).

### 채워진 입력 파싱
```powershell
python C:\Works\auto-tennis-bracket\.claude\skills\tennis-input-template\scripts\parse_input.py --in <input.xlsx> --out C:\Works\auto-tennis-bracket\_workspace\01_parsed.json
```

파싱 결과 JSON 스키마는 `requirements-parser` 에이전트 정의 참조.

## 검증 규칙 (parse_input.py 자체 수행)

실패 케이스는 stderr에 한국어로 명확히 출력 + exit code 1:
- 필수 컬럼 누락
- 이름 중복
- 시간 형식 오류 (HH:MM 아님, 30분 단위 아님)
- IN ≥ OUT
- 성별이 "남"/"여" 아님
- 구분이 "정회원"/"게스트" 아님
- 최소게임수/최대게임수가 1 미만이거나 정수 아님, 최소게임수 > 최대게임수
- 코트의 시작 ≥ 종료
- 참가자 < 4명

경고 케이스는 stderr에 "[경고]" 접두로 출력 + 정상 진행:
- 남자(여자) 4명 미만 → 단성 복식 불가 안내
- 특정 시간대 가용 인원 4명 미만
- 코트 운영 시간이 모든 참가자 IN 범위 밖
- 최소게임수 > 본인 가용 슬롯수 (가용 슬롯 수까지만 보장)
- 최소게임수 합계 > 전체 배정 가능 자리 (일부 보장 불가 가능)

## 셀 서식 (build_template.py 산출)
- 헤더 행: 굵게 + 회색 배경 + 가운데 정렬
- 데이터 셀: 가운데 정렬
- 시간 컬럼: 텍스트 형식 (HH:MM 그대로 입력받기 위해)
- 컬럼 너비: 자동 조정
- `안내` 시트에 워크플로우 설명 + 예시
