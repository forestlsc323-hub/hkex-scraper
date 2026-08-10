@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HKEX Scraper - 更新到最新版

REM 从 GitHub 拉最新代码覆盖本文件夹。
REM data\ 和 logs\ 不动 —— 那是你跑出来的东西，覆盖了就没了。
REM .venv\ 也不动，不然每次更新都要重装依赖。

set "BRANCH=claude/hkex-disclosure-data-extraction-unftw7"
set "FOLDER=hkex-scraper-claude-hkex-disclosure-data-extraction-unftw7"
set "URL=https://codeload.github.com/forestlsc323-hub/hkex-scraper/zip/refs/heads/%BRANCH%"
set "TMPZIP=%TEMP%\hkex_update.zip"
set "TMPDIR=%TEMP%\hkex_update"

echo.
echo   正在下载最新版...
echo.

if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
if exist "%TMPZIP%" del /q "%TMPZIP%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri $env:URL -OutFile $env:TMPZIP -UseBasicParsing } catch { exit 1 }"
if not %errorlevel%==0 goto DOWNLOAD_FAILED
if not exist "%TMPZIP%" goto DOWNLOAD_FAILED

echo   正在解压...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -Path $env:TMPZIP -DestinationPath $env:TMPDIR -Force } catch { exit 1 }"
if not %errorlevel%==0 goto UNZIP_FAILED
if not exist "%TMPDIR%\%FOLDER%\app.py" goto UNZIP_FAILED

echo   正在覆盖程序文件（data 和 logs 不动）...
robocopy "%TMPDIR%\%FOLDER%" "%~dp0." /E /XD data logs .venv .git /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% geq 8 goto COPY_FAILED

rmdir /s /q "%TMPDIR%" 2>nul
del /q "%TMPZIP%" 2>nul

echo.
echo   [OK] 已更新到最新版。
echo.
echo   现在双击「一键运行」就是新版本了。
echo.
pause
exit /b 0

:DOWNLOAD_FAILED
echo.
echo   [X] 下载失败。
echo.
echo       多半是网络不通，或者公司网络挡了 github.com。
echo       换个网络再试；实在不行就还用老办法：
echo       去 GitHub 网页 - Code - Download ZIP - 解压覆盖这个文件夹。
echo.
pause
exit /b 1

:UNZIP_FAILED
echo.
echo   [X] 解压失败，下载到的文件可能不完整。
echo       重新双击这个文件再试一次。
echo.
pause
exit /b 1

:COPY_FAILED
echo.
echo   [X] 复制文件失败。
echo.
echo       最常见的原因：程序还开着。请先关掉抓取工具的窗口，再试一次。
echo.
pause
exit /b 1

