@echo off
chcp 65001 >nul
cd /d "%~dp0"
python app.py %*
if errorlevel 1 (
  echo.
  echo 실행에 실패했습니다. 파이썬이 설치돼 있는지 확인하세요: https://www.python.org/downloads/
  pause
)
