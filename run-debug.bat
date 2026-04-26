@echo off
chcp 65001 >nul
cd /d %~dp0
echo [调试模式] 如 GUI 崩溃或不显示，此窗口会显示 Python 错误
python tunnel_gui.py
pause
