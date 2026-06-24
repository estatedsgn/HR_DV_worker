@echo off
REM Дайвинчик авто-свайпер. Двойной клик — запускает бота 24/7 (окно 10:00-21:00).
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" -m app.services.daivinchik
echo.
echo Бот остановился. Перезапуск через 10 секунд (Ctrl+C чтобы выйти)...
timeout /t 10 >nul
goto loop
