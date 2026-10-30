@echo off
rem Launch the KiriCompass precedent viewer (Windows double-click).
rem Resolves everything relative to this file, so the repo can live anywhere.
rem Fallback for when KiriCompass.pyw does nothing: first import the viewer
rem with a console Python (3.10+, tkinter, kifudb) so a failure is shown here
rem and the window waits; on success start it with pythonw from the same
rem installation and close this window.
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo Python 3.10 or later was not found. Install it from python.org.
    pause
    exit /b 1
)
%PY% -c "import sys; sys.path.insert(0, 'tools'); sys.exit('KiriCompass needs Python 3.10 or later (found ' + sys.version.split()[0] + ')') if sys.version_info < (3, 10) else None; import precedent_gui"
if errorlevel 1 (
    pause
    exit /b 1
)
set "PYW="
for /f "delims=" %%i in ('%PY% -c "import sys, pathlib; w = pathlib.Path(sys.executable).with_name('pythonw.exe'); print(w if w.is_file() else sys.executable)"') do set "PYW=%%i"
if not defined PYW set "PYW=pythonw"
start "" "%PYW%" tools\precedent_gui.py %*
