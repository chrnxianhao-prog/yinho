@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境，请先按照 README 安装依赖。
  pause
  exit /b 1
)
echo 正在启动 MT5 终端和银禾量化桌面工作台...
".venv\Scripts\python.exe" "scripts\run_desktop.py"
if errorlevel 1 pause
