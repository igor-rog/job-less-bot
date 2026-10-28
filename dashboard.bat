@echo off
rem Double-click to open the dashboard in your browser. Keep this window open while you use it.
cd /d "%~dp0"
rem headless: skip Streamlit's first-run email question; the browser is opened here instead, once the server is up
start "" /min cmd /c "timeout /t 4 >nul & start http://localhost:8501"
.venv\Scripts\streamlit.exe run dashboard.py --server.headless true
pause
