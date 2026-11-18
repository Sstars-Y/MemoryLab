@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".local\runtime\venv\Scripts\python.exe" (
  echo 尚未准备环境。请先双击“准备环境.cmd”。
  pause
  exit /b 1
)
set "TEMP=%~dp0.local\tmp"
set "TMP=%~dp0.local\tmp"
set "HF_HOME=%~dp0.local\caches\huggingface"
set "TORCH_HOME=%~dp0.local\caches\torch"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "OPENBLAS_NUM_THREADS=4"
set "OMP_NUM_THREADS=8"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
if not exist "%TEMP%" mkdir "%TEMP%"
".local\runtime\venv\Scripts\python.exe" -m memory_lab --data-root "%~dp0.local\data" %*
if errorlevel 1 pause
endlocal
