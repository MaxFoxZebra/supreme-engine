@echo off
REM Start the development server from anywhere, with the right interpreter.
cd /d "%~dp0"
python dev.py %*
