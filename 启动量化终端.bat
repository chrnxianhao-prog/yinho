@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境，请先按照 README 安装依赖。
  pause
  exit /b 1
)
echo 正在启动多市场纸面交易终端...
echo 关闭此窗口即可停止本地服务。
".venv\Scripts\python.exe" "scripts\run_web.py" --host 127.0.0.1 --port 8787 --open-browser
pause
