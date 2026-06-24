@echo off
chcp 65001 >nul
title Douyin 下载浏览

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Nouveau\douyin_email_bot\launch_browser.ps1"

:: If powershell exited with error, keep window open so user can read the message
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo 按任意键关闭...
    pause >nul
)
