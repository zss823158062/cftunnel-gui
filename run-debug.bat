@echo off
chcp 65001 >nul
cd /d %~dp0
echo [开发/调试模式] 启动 Tauri + Vite 开发服务器与控制台输出...
pnpm tauri dev
pause
