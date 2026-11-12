@echo off
rem publish.bat  (owner / dev only)
rem Commits everything in this folder and pushes it to GitHub (branch main).
rem Same approach as CAD Modeler's "Publish Changes.bat": plain git, and Git
rem Credential Manager signs you in (browser) the first time.
rem First run: asks for the GitHub repo URL and points install.bat + the
rem auto-updater at it.   "publish.bat /fast" skips the tests.
setlocal EnableDelayedExpansion
title Publish Shared Cockpit

rem A pull/rebase can rewrite this very file while cmd is still reading it,
rem so run from a temporary copy and pass the project folder along.
if /i "%~1"=="--run" goto :relaunched
copy /y "%~f0" "%TEMP%\shared-cockpit-publish.bat" >nul 2>&1 && "%TEMP%\shared-cockpit-publish.bat" --run "%~dp0" %1
set "ROOT=%~dp0"
set "OPT=%~1"
goto :begin
:relaunched
set "ROOT=%~2"
set "OPT=%~3"
:begin
cd /d "%ROOT%"
if errorlevel 1 (
  echo ERROR: could not open the project folder: %ROOT%
  goto :fail
)

rem ---- Git and Python installed? ----------------------------------------------
where git >nul 2>&1
if errorlevel 1 (
  echo ERROR: Git is not installed or not on PATH.
  echo Install it from https://git-scm.com/download/win and run this again.
  goto :fail
)
set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=py -3"

rem ---- Is this folder a repo? ----------------------------------------------
if exist ".git\" goto :haverepo
echo This folder is not a git repository yet - creating one.
git init -q
if errorlevel 1 goto :fail
:haverepo
if exist ".git\rebase-merge\" goto :midrebase
if exist ".git\rebase-apply\" goto :midrebase

rem ---- Make sure we are on branch main --------------------------------------
set "BRANCH="
for /f "delims=" %%B in ('git symbolic-ref --short HEAD 2^>nul') do set "BRANCH=%%B"
if not defined BRANCH (
  echo ERROR: git is not on a branch right now - detached HEAD.
  echo Run "git switch main" in this folder, then try again.
  goto :fail
)
if "!BRANCH!"=="main" goto :onmain
git rev-parse --verify -q HEAD >nul 2>&1
if errorlevel 1 (
  git symbolic-ref HEAD refs/heads/main
) else (
  echo Renaming branch !BRANCH! to main...
  git branch -M main
)
if errorlevel 1 goto :fail
:onmain

rem ---- origin remote --------------------------------------------------------
set "ORIGIN="
for /f "delims=" %%U in ('git remote get-url origin 2^>nul') do set "ORIGIN=%%U"
if defined ORIGIN goto :haveorigin
echo.
echo No GitHub repo is linked yet.
echo 1. Create a new PUBLIC repository at https://github.com/new
echo    - public, so friends' install.bat can download it
echo    - leave it empty if you can; files already in it get replaced by this folder
echo 2. Copy its URL, e.g. https://github.com/your-name/shared-cockpit.git
echo.
set /p "ORIGIN=Paste the GitHub repo URL: "
if not defined ORIGIN (
  echo ERROR: no URL entered.
  goto :fail
)
set "ORIGIN=!ORIGIN:"=!"
set "ORIGIN=!ORIGIN: =!"
git remote add origin "!ORIGIN!"
if errorlevel 1 goto :fail
echo Remote origin set to !ORIGIN!
:haveorigin
echo Publishing to: !ORIGIN!
rem install.bat and the updater download from this repo
%PY% tools\set_repo.py "!ORIGIN!"
if errorlevel 1 goto :fail
rem Credential Manager: go straight to the browser sign-in (this repo only)
git config credential.gitHubAuthModes browser

rem ---- Commit identity (git refuses to commit without one) ------------------
set "GNAME="
set "GMAIL="
for /f "delims=" %%N in ('git config user.name 2^>nul') do set "GNAME=%%N"
for /f "delims=" %%E in ('git config user.email 2^>nul') do set "GMAIL=%%E"
if defined GNAME if defined GMAIL goto :haveident
echo.
echo Git needs a name and email to label your commits - one-time setup.
if not defined GNAME set /p "GNAME=Your name: "
if not defined GMAIL set /p "GMAIL=Your email: "
if not defined GNAME goto :noident
if not defined GMAIL goto :noident
git config --global user.name "!GNAME!"
git config --global user.email "!GMAIL!"
:haveident

rem ---- Tests ----------------------------------------------------------------
if /i "!OPT!"=="/fast" goto :skiptests
echo.
echo Running tests (about a minute; "publish.bat /fast" skips them)...
set "FAILED="
for %%t in (loopback dryrun smooth updater) do (
  %PY% tests\test_%%t.py >"%TEMP%\sc_test_%%t.log" 2>&1
  if errorlevel 1 (
    echo   %%t  FAILED  - log: %TEMP%\sc_test_%%t.log
    set "FAILED=1"
  ) else (
    echo   %%t  ok
  )
)
if defined FAILED (
  choice /c YN /n /m "Tests failed. Publish anyway? [Y/N] "
  if errorlevel 2 goto :fail
)
:skiptests

rem ---- First publish into a repo that already has files ----------------------
rem (e.g. uploaded on the GitHub website): put this folder's files on top of
rem that history, so the push is a normal update - no force push, history kept.
git rev-parse --verify -q HEAD >nul 2>&1
if not errorlevel 1 goto :hashistory
git ls-remote --exit-code --heads origin main >nul 2>&1
if errorlevel 1 goto :hashistory
echo.
echo The GitHub repo already has files. This folder's version will replace them
echo (their history is kept).
git fetch -q origin main
if errorlevel 1 goto :fail
git reset -q --soft FETCH_HEAD
if errorlevel 1 goto :fail
:hashistory

rem ---- Show and commit changes ---------------------------------------------
echo.
git add -A
if errorlevel 1 goto :fail
set "DIRTY="
for /f "delims=" %%L in ('git status --porcelain') do set "DIRTY=1"
if not defined DIRTY (
  echo No changed files - nothing new to commit.
  goto :aftercommit
)
echo Changed files:
git status --short
echo.
set "VER="
choice /c YN /n /m "Bump the version so installed copies get an update prompt? [Y/N] "
if not errorlevel 2 (
  for /f "delims=" %%v in ('%PY% tools\bump_version.py patch') do set "VER=%%v"
  git add -A
  echo Version is now !VER!
)
set "DEFMSG="
for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'" 2^>nul') do set "DEFMSG=Update %%T"
if not defined DEFMSG set "DEFMSG=Update %date% %time:~0,5%"
set "MSG="
set /p "MSG=Commit message [Enter = "!DEFMSG!"]: "
if not defined MSG set "MSG=!DEFMSG!"
if defined VER set "MSG=v!VER!: !MSG!"
set "MSGFILE=%TEMP%\shared-cockpit-commit-msg.txt"
> "!MSGFILE!" echo(!MSG!
git commit -q -F "!MSGFILE!"
if errorlevel 1 (
  del "!MSGFILE!" >nul 2>&1
  echo ERROR: the commit failed. See the message above.
  goto :fail
)
del "!MSGFILE!" >nul 2>&1
echo Committed: !MSG!
:aftercommit

git rev-parse --verify -q HEAD >nul 2>&1
if errorlevel 1 (
  echo Nothing has been committed yet, so there is nothing to publish.
  goto :end
)

rem ---- Sync with GitHub, then push -----------------------------------------
echo.
echo Checking GitHub...
git ls-remote --exit-code --heads origin main >nul
set "RC=!errorlevel!"
if "!RC!"=="2" (
  echo The GitHub repo is empty - this will be the first upload.
  goto :push
)
if not "!RC!"=="0" (
  echo.
  echo ERROR: could not reach the GitHub repo !ORIGIN!
  echo  - check your internet connection
  echo  - check the URL: "git remote set-url origin NEW-URL" fixes a wrong one
  echo  - sign in when the Git Credential Manager window pops up
  goto :fail
)
echo Getting changes already on GitHub...
git pull -q --rebase origin main
if errorlevel 1 goto :pullfailed

:push
echo Uploading... (the first time, a GitHub sign-in opens in your browser)
git push -u origin main
if errorlevel 1 (
  echo.
  echo ERROR: the push failed. Your commit is saved locally; fix the problem
  echo above and run this file again to retry.
  goto :fail
)
echo.
echo Done - your changes are on GitHub. Friends get them through install.bat,
echo and installed copies offer the update when they start.
goto :end

:pullfailed
echo.
if exist ".git\rebase-merge\" goto :abortrebase
if exist ".git\rebase-apply\" goto :abortrebase
echo ERROR: could not pull from GitHub. Your commit is saved locally.
echo If it says "unrelated histories", the GitHub repo was started separately:
echo ask Claude, or run  git pull --rebase --allow-unrelated-histories origin main
goto :fail
:abortrebase
git rebase --abort >nul 2>&1
echo ERROR: your changes conflict with changes already on GitHub.
echo The pull was undone; your commit is still saved locally.
echo Resolve it by hand: git pull --rebase origin main, fix the files,
echo git add them, git rebase --continue, then run this file again.
goto :fail

:midrebase
echo ERROR: a git rebase is in progress in this folder.
echo Finish it with "git rebase --continue" or cancel it with "git rebase --abort",
echo then run this file again.
goto :fail

:noident
echo ERROR: a name and email are both required to commit.
goto :fail

:fail
echo.
echo Publish did not finish.
pause
exit /b 1

:end
echo.
pause
exit /b 0
