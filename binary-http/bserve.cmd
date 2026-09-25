@echo off
rem .\bserve.cmd .\www 9000   (Windows launcher for bserve.py)
rem Asks the py launcher for the interpreter path, so the script's shebang line is never consulted.
setlocal
set "PYEXE="
for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" "%~dp0bserve.py" %*
exit /b %errorlevel%
