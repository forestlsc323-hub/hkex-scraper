@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   HKEX 披露易抓取工具
echo   %CD%
echo ============================================================
echo.

REM ---------- 找一个真的能用的 Python ----------
set PY=
for %%C in ("py -3" "python" "python3") do (
  %%~C -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set PY=%%~C
    goto :found
  )
)
echo [X] 没找到可用的 Python。
echo.
echo     去这里下载安装： https://www.python.org/downloads/
echo     安装时务必勾选最下面那个 "Add Python to PATH"
echo     装完关掉这个窗口，重新双击本文件。
echo.
echo     注意：如果你输 python 会弹出微软商店，说明装的是商店版占位符，
echo     请到 设置 - 应用 - 应用执行别名，把 python.exe 关掉，再装官网版。
echo.
pause & exit /b 1

:found
echo Python: & %PY% --version
echo.

REM ---------- 虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在创建虚拟环境（约 10 秒）...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 创建虚拟环境失败。把上面的报错发给 Claude。
    pause & exit /b 1
  )
)
set VPY=.venv\Scripts\python.exe

REM ---------- 依赖 ----------
echo 检查依赖...
%VPY% -c "import requests,yaml,bs4,pdfplumber" >nul 2>&1
if errorlevel 1 (
  echo   正在安装，首次约 1-2 分钟，请等它跑完...
  %VPY% -m pip install --upgrade pip
  %VPY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [X] 安装依赖失败。把上面的报错发给 Claude。
    pause & exit /b 1
  )
)
echo   依赖就绪
echo.

REM ---------- 主流程（Python 全程写日志，窗口关了也不丢） ----------
%VPY% run_all.py %*
set RC=%errorlevel%

echo.
echo ------------------------------------------------------------
echo  这两个文件已经存到文件夹里，关掉窗口也还在：
echo    运行日志.txt      完整过程（出错时的堆栈在里面）
echo    发给CLAUDE.txt    发给 Claude 的诊断
echo ------------------------------------------------------------
echo.

if "%RC%"=="0" (
  if exist "data\screening\report.html" (
    echo 正在打开结果网页...
    start "" "data\screening\report.html"
  )
)

echo 现在可以关掉这个窗口了（或按任意键关闭）。
pause >nul
