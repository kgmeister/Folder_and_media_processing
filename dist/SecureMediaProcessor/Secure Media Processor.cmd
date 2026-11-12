@echo off
rem Secure Media Processor - portable. Starts the app from this folder; nothing is installed.
start "" "%~dp0runtime\pythonw.exe" -E -s "%~dp0app\app.py" %*
