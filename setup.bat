@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo    GTA6-WATCH  -  one-shot setup
echo ============================================
echo.

echo [1/6] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 goto fail
echo.

echo [2/6] Running self-test...
python monitor.py --selftest
if errorlevel 1 goto fail
echo.

echo [3/6] Capturing first live baseline from the official site...
python monitor.py --init
echo.

echo [4/6] Initialising git...
git init
git add .
git commit -m "gta6-watch: initial deploy"
git branch -M main
echo.

echo [5/6] Create + push to GitHub...
set /p VIS="Make the repo public or private? [public/private] (default public): "
if "%VIS%"=="" set VIS=public
where gh >nul 2>nul
if %errorlevel%==0 (
  echo Found GitHub CLI - creating repo and pushing...
  gh repo create gta6-watch --%VIS% --source=. --push
) else (
  echo.
  echo GitHub CLI not found. Finish these two lines yourself:
  echo    git remote add origin https://github.com/YOUR_USERNAME/gta6-watch.git
  echo    git push -u origin main
)
echo.

echo [6/6] One-time clicks in your browser after the push:
echo    - Actions tab: enable workflows if prompted
echo    - Settings ^> Pages ^> Deploy from branch ^> main ^> /docs ^> Save
echo    - Optional: Settings ^> Secrets ^> Actions ^> add ALERT_WEBHOOK for Discord/Slack
echo.
echo All set. Open docs\index.html to view the dashboard locally any time.
goto end

:fail
echo.
echo Something failed above. Fix it and re-run, or paste the error to Claude.

:end
echo.
pause
