@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 run.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw run.py & exit /b)
python run.py
pause
