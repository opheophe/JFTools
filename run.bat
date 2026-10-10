@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ============================================
echo   MedServ launcher
echo  ============================================
echo.

if exist ".venv\Scripts\python.exe" goto have_venv

echo [1/2] Creating virtual environment .venv ...
python -m venv .venv
if errorlevel 1 goto error

echo [2/2] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error
goto run

:have_venv
echo Using existing virtual environment .venv.

:run
echo.
echo Starting MedServ at  http://localhost:8099
echo Press Ctrl+C to stop.
echo.

start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & start http://localhost:8099"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --reload
goto :eof

:error
echo.
echo Something went wrong. Check the messages above.
echo Make sure Python 3 is installed and on your PATH.
pause
exit /b 1
