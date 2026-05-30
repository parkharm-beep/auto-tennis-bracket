@echo off
chcp 65001 > nul
setlocal

REM 사용법:
REM   대진표_생성.bat                            (대화형 — 날짜만 묻고 자동 실행)
REM   대진표_생성.bat --create-template          (빈 입력 양식 생성)
REM   대진표_생성.bat --create-template --prefill image  (이미지 사전채움 양식)
REM   대진표_생성.bat --date 26.5.30             (인자 직접 전달)
REM
REM 파일 위치:
REM   입력 양식: 입력\테니스_입력양식.xlsx
REM   결과 파일: 출력\테니스_대진표_<날짜>.xlsx

cd /d "%~dp0"

if "%~1"=="" goto INTERACTIVE
if "%~1"=="-h" goto HELP
if "%~1"=="--help" goto HELP

REM 인자 그대로 전달
python "%~dp0대진표_생성.py" %*
goto END

:INTERACTIVE
echo.
echo === 테니스 대진표 생성기 ===
echo.
echo 입력 양식: 입력\테니스_입력양식.xlsx
echo.
set /p DATE_STR="대진표 날짜 (예: 26.5.30, 비우면 오늘): "
python "%~dp0대진표_생성.py" --date "%DATE_STR%"
goto END

:HELP
python "%~dp0대진표_생성.py" --help

:END
echo.
pause
