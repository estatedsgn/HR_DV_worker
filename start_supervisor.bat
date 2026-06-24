@echo off
REM Пульт управления агентом Profitcast. Двойной клик — запускает Telegram-бота.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m app.supervisor
echo.
echo Пульт остановлен. Нажми любую клавишу, чтобы закрыть окно.
pause >nul
