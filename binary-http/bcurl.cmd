@echo off
rem .\bcurl.cmd -v localhost:9000/index.html   (Windows launcher for bcurl.py)
rem Asks the py launcher for the interpreter path, so the script's shebang line is never consulted.
setlocal
set "PYEXE="
for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" "%~dp0bcurl.py" %*
exit /b %errorlevel%
