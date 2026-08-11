@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HKEX Scraper - 更新到最新版

REM 这个文件现在只做一件事：让程序自己去更新自己。
REM
REM 原来它是自己下载 zip、解压、覆盖文件的（cmd 调 PowerShell 调
REM Invoke-WebRequest）。功能没毛病，但「一个脚本从互联网取内容并就地
REM 覆盖可执行文件」正是下载器木马的行为特征 —— 卡巴斯基把它判成
REM PDM:Trojan.Win32.Generic.nblk 直接删了。杀软没冤枉它。
REM
REM 所以别再跟启发式引擎较劲：下载和覆盖都交给 hkexdb\updater.py，
REM 走程序本来就在用的那条网络路径。
REM
REM 更省事的办法：直接开程序，「抓取」页右下角有个「检查更新」按钮，
REM 效果一模一样，也不需要这个 .bat。

if not exist ".venv\Scripts\python.exe" (
  echo [X] 还没装好环境。请先双击 一键运行.bat 跑一次。
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m hkexdb.updater
echo.
pause
