@echo off
cd /d "%~dp0"
if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" app.py
) else (
    echo 没有找到 venv，请先执行：
    echo   python -m venv venv
    echo   venv\Scripts\python -m pip install requests pywebview
    pause
)
