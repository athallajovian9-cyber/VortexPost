@echo off
title VortexPost
cd /d "%~dp0"
python -c "import customtkinter, PIL" 2>nul || (
  echo Installing dependencies ^(one time^)...
  python -m pip install -r requirements.txt
)
start "" pythonw vortexpost.py
if errorlevel 1 python vortexpost.py
