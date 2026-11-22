@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=py"
py -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul
if not errorlevel 1 goto run
set "PYTHON=python"
python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>nul
if errorlevel 1 goto error
:run
%PYTHON% -c "import pymupdf, reportlab, PIL" >nul 2>nul
if not errorlevel 1 goto start
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto error
:start
%PYTHON% launcher.py
if errorlevel 1 goto error
goto :eof
:error
echo Python 3.10+ or required packages could not be started.
pause
