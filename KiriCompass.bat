@echo off
rem Launch the KiriCompass precedent viewer (Windows double-click).
rem Resolves everything relative to this file, so the repo can live anywhere.
rem Prefer pythonw (no console window); fall back to python / the py launcher.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw tools\precedent_gui.py %* & exit /b)
where python  >nul 2>nul && (python tools\precedent_gui.py %* & exit /b)
py -3 tools\precedent_gui.py %*
if errorlevel 1 pause
