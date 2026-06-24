@echo off
REM Автономная связка: Дайвинчик-свайпер + автопилот воронки.
REM Двойной клик — поднимает оба процесса 24/7 с авто-перезапуском.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" scripts\run_autonomous.py
echo.
echo Раннер остановился. Перезапуск через 10 секунд (Ctrl+C чтобы выйти)...
timeout /t 10 >nul
goto loop
