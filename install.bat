@echo off
rem ===================================================================
rem  Shared Cockpit - one-file installer / updater for Windows
rem  Download just this file and double-click it. It will:
rem    1. install Python if you don't have it (winget, else python.org)
rem    2. download Shared Cockpit from GitHub into %LOCALAPPDATA%\SharedCockpit
rem    3. detect MSFS 2020 / 2024 and fetch SimConnect from Microsoft
rem    4. add Desktop + Start Menu shortcuts and launch the app
rem  Run it again any time to repair/update.  "install.bat /uninstall" removes it.
rem ===================================================================
setlocal EnableExtensions
set "REPO=Laws0fPhysics1/Shared-Cockpit"
set "BRANCH=main"
set "DEST=%LOCALAPPDATA%\SharedCockpit"
title Shared Cockpit setup

if /i "%~1"=="/uninstall" goto :uninstall

echo.
echo  ==== Shared Cockpit setup ====
echo.
call :findpy
if not defined PY call :installpy
if not defined PY goto :nopython
echo  Python: %PY%

rem --- get the app files ------------------------------------------------
set "LOCALSRC="
if exist "%~dp0run.py" if exist "%~dp0shared_cockpit\engine.py" set "LOCALSRC=%~dp0"
if defined LOCALSRC goto :fromlocal

if "%REPO:~0,6%"=="OWNER/" (
  echo  This installer has no GitHub repo set. Edit REPO at the top of install.bat.
  goto :fail
)
echo  Downloading Shared Cockpit from github.com/%REPO% ...
set "ZIP=%TEMP%\sharedcockpit_%RANDOM%.zip"
set "UNZ=%TEMP%\sharedcockpit_%RANDOM%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing 'https://github.com/%REPO%/archive/refs/heads/%BRANCH%.zip' -OutFile '%ZIP%'; Expand-Archive -Force '%ZIP%' '%UNZ%'"
if errorlevel 1 (
  echo  Download failed. Check your internet connection and try again.
  goto :fail
)
for /d %%d in ("%UNZ%\*") do set "SRC=%%d"
robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo  Copying files failed.
  goto :fail
)
del "%ZIP%" >nul 2>nul
rmdir /s /q "%UNZ%" >nul 2>nul
goto :installed

:fromlocal
if /i "%LOCALSRC%"=="%DEST%\" goto :installed
echo  Installing from local folder %LOCALSRC%
robocopy "%LOCALSRC%." "%DEST%" /E /XD .git __pycache__ /XF settings.json /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo  Copying files failed.
  goto :fail
)

:installed
echo  Installed to %DEST%
echo.
"%PY%" "%DEST%\run.py" --setup
echo.
echo  ==== Done! Use the "Shared Cockpit" shortcut on your Desktop. ====
if "%SC_NOLAUNCH%"=="1" goto :eof
for %%p in ("%PY%") do set "PYW=%%~dpppythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%DEST%\run.py"
timeout /t 4 >nul
goto :eof

rem --- helpers -------------------------------------------------------------
:findpy
set "PY="
for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable) if sys.version_info >= (3, 10) else None" 2^>nul') do set "PY=%%p"
if defined PY goto :eof
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable) if sys.version_info >= (3, 10) else None" 2^>nul') do set "PY=%%p"
if defined PY goto :eof
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python31*") do if exist "%%d\python.exe" set "PY=%%d\python.exe"
goto :eof

:installpy
echo  Python not found - installing Python 3.12 for your user account (one time)...
where winget >nul 2>nul
if not errorlevel 1 winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto :eof
echo  Trying the python.org installer instead...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $f=Join-Path $env:TEMP 'python-3.12.8-amd64.exe'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $f; Start-Process $f -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1' -Wait; Remove-Item $f"
call :findpy
goto :eof

:nopython
echo.
echo  Could not install Python automatically.
echo  Install Python 3.12 from https://www.python.org/downloads/ - tick "Add python.exe to PATH" -
echo  then run this installer again.
goto :fail

:uninstall
call :findpy
if defined PY if exist "%DEST%\run.py" "%PY%" "%DEST%\run.py" --remove-shortcuts
if exist "%DEST%" rmdir /s /q "%DEST%"
echo  Shared Cockpit removed.
pause
goto :eof

:fail
echo.
pause
exit /b 1
