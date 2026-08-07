@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   港交所披露易抓取工具
echo   工作目录: %CD%
echo ============================================================
echo.

REM ---------- 找 Python ----------
set PY=
where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" (where python >nul 2>&1 && set PY=python)
if "%PY%"=="" (
  echo [X] 没找到 Python。
  echo     去 https://www.python.org/downloads/ 装一个，
  echo     安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
  pause & exit /b 1
)
echo [1/6] Python: & %PY% --version

REM ---------- 建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
  echo [2/6] 首次运行，正在创建虚拟环境...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [X] 创建失败 & pause & exit /b 1 )
) else (
  echo [2/6] 虚拟环境已存在
)
set VPY=.venv\Scripts\python.exe

REM ---------- 装依赖 ----------
echo [3/6] 检查依赖...
%VPY% -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if errorlevel 1 (
  echo       正在安装（首次约 1-2 分钟）...
  %VPY% -m pip install -q --upgrade pip
  %VPY% -m pip install -q -r requirements.txt
  if errorlevel 1 ( echo [X] 安装失败，把上面的报错发给 Claude & pause & exit /b 1 )
)
echo       依赖就绪

REM ---------- 抓列表 ----------
echo.
echo [4/6] 抓取公告列表（直接用 vendor\hkex_client.py，你那份客户端）
echo       这一步最久，按 config.yaml 的日期范围，几分钟到十几分钟
echo.
%VPY% run_vendor.py
set FETCH_RC=%errorlevel%

REM ---------- 筛查 + 网页 ----------
if "%FETCH_RC%"=="0" (
  echo.
  echo [5/6] 质控筛查
  %VPY% run_screening.py data\raw\vendor_listing.csv
  echo.
  echo [6/6] 生成网页
  %VPY% run_report.py
) else (
  echo.
  echo [!] 抓取没成功，跳过后面两步。
)

REM ---------- 打包诊断 ----------
echo.
echo ============================================================
%VPY% collect_result.py
echo ============================================================
echo.
if "%FETCH_RC%"=="0" (
  echo 完成。用浏览器打开这个文件看结果：
  echo   %CD%\data\screening\report.html
  echo.
)
echo 把 发给CLAUDE.txt 这个文件的内容发给 Claude。
echo.
pause
