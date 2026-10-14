@echo off
cd /d %~dp0
if exist "src-tauri\target\release\cftunnel-gui.exe" (
    start "" "src-tauri\target\release\cftunnel-gui.exe"
    exit /b
)
if exist "src-tauri\target\debug\cftunnel-gui.exe" (
    start "" "src-tauri\target\debug\cftunnel-gui.exe"
    exit /b
)
pnpm tauri dev
