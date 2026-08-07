@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   HKEX Disclosure Scraper
echo   %CD%
echo ============================================================
echo.

set "PY="
py -3 -c "import sys" >nul 2>&1
if %errorlevel%==0 set "PY=py -3"

if not defined PY (
  python -c "import sys" >nul 2>&1
  if %errorlevel%==0 set "PY=python"
)

if not defined PY (
  echo [X] Python not found / 没找到可用的 Python
  echo.
  echo     Download: https://www.python.org/downloads/
  echo     安装时务必勾选最下面的 "Add Python to PATH"
  echo.
  echo     如果输 python 会弹出微软商店，说明是商店占位符：
  echo     设置 - 应用 - 高级应用设置 - 应用执行别名，关掉 python.exe
  echo.
  echo Press any key to close.
  pause >nul
  exit /b 1
)

echo Python:
%PY% --version
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment / 创建虚拟环境...
  %PY% -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo [X] venv failed / 创建虚拟环境失败
  echo     把上面的报错发给 Claude
  echo.
  echo Press any key to close.
  pause >nul
  exit /b 1
)

echo Checking dependencies / 检查依赖...
.venv\Scripts\python.exe -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if not %errorlevel%==0 (
  echo   Installing, first time takes 1-2 min / 首次安装约 1-2 分钟...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

.venv\Scripts\python.exe -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if not %errorlevel%==0 (
  echo [X] dependency install failed / 安装依赖失败
  echo     把上面的报错发给 Claude
  echo.
  echo Press any key to close.
  pause >nul
  exit /b 1
)
echo   OK
echo.

.venv\Scripts\python.exe run_all.py %*
set "RC=%errorlevel%"

echo.
echo ------------------------------------------------------------
echo  These files are saved in the folder / 这两个文件已存到文件夹：
echo    run_log.txt         完整过程（出错堆栈在里面）
echo    SEND_TO_CLAUDE.txt  发给 Claude 的诊断
echo ------------------------------------------------------------
echo.

if "%RC%"=="0" if exist "data\screening\report.html" start "" "data\screening\report.html"

echo Press any key to close / 按任意键关闭。
pause >nul
