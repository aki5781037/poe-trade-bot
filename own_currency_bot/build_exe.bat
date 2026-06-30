@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

set PYTHON_EXE=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
if not exist "%PYTHON_EXE%" (
  echo Python runtime not found: %PYTHON_EXE%
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  "%PYTHON_EXE%" -m venv .venv
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --name OwnCurrencyBot --console ^
  --collect-data rapidocr ^
  --add-binary "drivers\msdk.dll;drivers" ^
  --add-data "..\config.toml;." ^
  --add-data "..\images;images" ^
  currency_bot.py

echo.
echo Built: %cd%\dist\OwnCurrencyBot\OwnCurrencyBot.exe
pause
