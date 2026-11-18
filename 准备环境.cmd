@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo 未找到 uv。请先安装 uv，再重新运行。
  pause
  exit /b 1
)
set "UV_CACHE_DIR=%~dp0.local\caches\uv"
set "UV_PYTHON_INSTALL_DIR=%~dp0.local\runtime\python"
set "UV_PROJECT_ENVIRONMENT=%~dp0.local\runtime\venv"
set "TEMP=%~dp0.local\tmp\setup"
set "TMP=%~dp0.local\tmp\setup"
if not exist "%TEMP%" mkdir "%TEMP%"
uv python install 3.12.15 --no-bin --no-registry
if errorlevel 1 goto failed
uv sync --locked --python "%UV_PYTHON_INSTALL_DIR%\cpython-3.12.15-windows-x86_64-none\python.exe" --group dev --extra embedding
if errorlevel 1 goto failed
echo 核心环境已准备。可双击启动实验版。
echo 本地语义检索还需要下载公开模型；请按 README 的模型准备步骤执行。
pause
exit /b 0
:failed
echo 环境准备失败，请保留上方错误信息后重试。
pause
exit /b 1
