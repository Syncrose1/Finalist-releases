@echo off
rem The Finalist Node on Windows, in this window: it logs here, and Ctrl+C stops it.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -u -m finalist_node) || (python -u -m finalist_node)
pause
