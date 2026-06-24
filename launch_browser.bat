@echo off
title Douyin File Browser

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Nouveau\douyin_email_bot\launch_browser.ps1"

if %ERRORLEVEL% NEQ 0 (
    echo.
    pause
)
