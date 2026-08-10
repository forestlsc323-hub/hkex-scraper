@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HKEX Scraper

set "PY="
py -3 -c "import sys" >nul 2>&1
if %errorlevel%==0 set "PY=py -3"

if not defined PY (
  python -c "import sys" >nul 2>&1
  if %errorlevel%==0 set "PY=python"
)

if not defined PY (
  echo [X] Python not found / 没找到 Python
  echo.
  echo     下载安装：https://www.python.org/downloads/
  echo     安装时务必勾选最下面的 "Add Python to PATH"
  echo.
  echo     如果输 python 会弹出微软商店：
  echo     设置 - 应用 - 高级应用设置 - 应用执行别名，关掉 python.exe
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating environment / 首次运行，正在准备环境...
  %PY% -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo [X] venv failed / 创建环境失败，把上面的报错发给 Claude
  pause
  exit /b 1
)

.venv\Scripts\python.exe -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if not %errorlevel%==0 (
  echo Installing dependencies, 1-2 min / 正在安装依赖，约 1-2 分钟...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

.venv\Scripts\python.exe -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if not %errorlevel%==0 (
  echo [X] install failed / 安装依赖失败，把上面的报错发给 Claude
  pause
  exit /b 1
)

start "" .venv\Scripts\pythonw.exe app.py
exit /b 0
