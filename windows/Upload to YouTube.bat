@echo off
REM Double-click to upload new videos from your folder to YouTube (runs inside WSL).
wsl.exe bash -lc "~/github/collab/upload.sh"
echo.
pause
