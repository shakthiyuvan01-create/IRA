@echo off
cd /d "%~dp0"
echo ============================================
echo   Starting IRA
echo ============================================
echo.
"%~dp0.venv\Scripts\python.exe" core\main.py
echo.
echo --------------------------------------------
echo   IRA has exited.
echo --------------------------------------------
pause
