@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   港交所披露易抓取工具
echo   %CD%
echo ============================================================
echo.

set PY=
where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" (where python >nul 2>&1 && set PY=python)
if "%PY%"=="" (
  echo [X] 没找到 Python。
  echo     装一个：https://www.python.org/downloads/
  echo     安装时务必勾选 "Add Python to PATH"，装完重新双击本文件。
  echo.
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在创建虚拟环境...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 创建虚拟环境失败。把上面的报错发给 Claude。
    pause & exit /b 1
  )
)
set VPY=.venv\Scripts\python.exe

echo 检查依赖...
%VPY% -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if errorlevel 1 (
  echo   正在安装（首次约 1-2 分钟）...
  %VPY% -m pip install -q --upgrade pip
  %VPY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [X] 安装依赖失败。把上面的报错发给 Claude。
    pause & exit /b 1
  )
)
echo   依赖就绪
echo.

REM 主流程全部交给 Python：屏幕和 运行日志.txt 同时写，窗口关了日志还在
%VPY% run_all.py %*
set RC=%errorlevel%

echo.
echo ------------------------------------------------------------
echo  即使现在关掉窗口，下面两个文件也还在：
echo    运行日志.txt      完整过程
echo    发给CLAUDE.txt    发给 Claude 的诊断
echo ------------------------------------------------------------
echo.

if "%RC%"=="0" (
  echo 正在打开结果网页...
  if exist "data\screening\report.html" start "" "data\screening\report.html"
)

echo 按任意键关闭窗口。
pause >nul
