@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JMGO dashboard

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python не найден. Установите его с https://www.python.org/downloads/ ^(галочка "Add python.exe to PATH"^).
  pause
  exit /b 1
)

echo IP:порт проектора из настроек беспроводной отладки, например 192.168.1.16:37123
echo ^(можно оставить пустым и нажать Enter - скрипт попробует найти проектор сам^)
set "TARGET="
set /p "TARGET=IP:порт: "

%PY% jmgo_dash.py %TARGET%
pause
