@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HKEX Scraper - 这个黑窗口别关，出错信息会显示在这里

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
  echo     请装 Python 3.13：
  echo       https://www.python.org/downloads/windows/
  echo       在左边 "Stable Releases" 里找最新的 Python 3.13.x
  echo       点它下面的 "Windows installer (64-bit)"
  echo       右边 "Pre-releases" 是测试版，不要选
  echo.
  echo     *** 安装第一屏最底下那个勾必须勾上 ***
  echo         [v] Add python.exe to PATH
  echo         不勾的话装完还是这个提示。
  echo.
  echo     装完关掉本窗口，重新双击本文件即可。
  echo.
  echo     如果输 python 会弹出微软商店（那是占位符，不是真 Python）：
  echo       设置 - 应用 - 高级应用设置 - 应用执行别名
  echo       把 python.exe 和 python3.exe 两个开关都关掉
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

echo.
echo 正在打开窗口... 这个黑框请留着，出错信息会显示在这里。
echo.

REM 用 python.exe 而不是 pythonw.exe：
REM pythonw 没有控制台，界面启动失败时你什么都看不到（静默失败）。
.venv\Scripts\python.exe app.py
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo.
  echo ------------------------------------------------------------
  echo  [X] 程序异常退出，代码 %RC%
  echo      上面的报错请截图，或把 app_crash.txt / run_log.txt 发给 Claude
  echo ------------------------------------------------------------
  pause
)
exit /b %RC%
