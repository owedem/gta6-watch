@echo off
cd /d "%~dp0"
echo ============================================
echo    Deploy to GitHub  (owedem/gta6-watch)
echo ============================================
echo.
echo Step 0: validating the bot before deploy...
python monitor.py --selftest
if errorlevel 1 (
  echo.
  echo SELF-TEST FAILED - not pushing. Paste the output above to Claude.
  pause
  exit /b 1
)
echo.
echo Step 1: set a git identity for this repo...
git config user.email "owedem@users.noreply.github.com"
git config user.name "owedem"
echo.
echo Step 2: point this folder at your GitHub repo...
git remote remove origin 2>nul
git remote add origin https://github.com/owedem/gta6-watch.git
echo.
echo Step 3: commit everything in this folder...
git add -A
git commit -m "deploy: dashboard data fix + latest monitor and config"
echo.
echo Step 4: push (force = make GitHub match this folder exactly)...
git branch -M main
git push -u --force origin main
echo.
echo If a GitHub sign-in window pops up, log in - that is normal the first time.
echo When you see 'Writing objects' with no red errors, you are live.
echo Wait about a minute for Pages, then Ctrl+F5 your dashboard.
echo.
pause
