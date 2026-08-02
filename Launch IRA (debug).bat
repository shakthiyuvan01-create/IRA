@echo off
cd /d "%~dp0"
echo ============================================
echo   Starting IRA in DEBUG mode
echo   (leave this window open; errors show here)
echo ============================================
echo.
"%~dp0.venv\Scripts\python.exe" core\main.py
echo.
echo --------------------------------------------
echo   IRA has exited.
echo   If there is an error above, copy it and send it.
echo --------------------------------------------
pause
