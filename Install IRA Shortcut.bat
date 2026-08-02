@echo off
setlocal
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"
echo Creating IRA desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $d=[Environment]::GetFolderPath('Desktop'); $s=$w.CreateShortcut((Join-Path $d 'IRA.lnk')); $s.TargetPath='wscript.exe'; $s.Arguments='\"%PROJ%\Launch IRA.vbs\"'; $s.WorkingDirectory='%PROJ%'; $s.IconLocation='%PROJ%\IRA.ico'; $s.Description='Launch IRA Assistant'; $s.Save()"
if errorlevel 1 ( echo Something went wrong creating the shortcut. & pause & exit /b 1 )
echo.
echo   Done!  An "IRA" shortcut with your logo is now on your Desktop.
echo   Double-click it any time to launch the assistant.
echo.
pause
