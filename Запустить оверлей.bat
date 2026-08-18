@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ===== 1. Ishchem ustanovlennyi Python (pythonw) =====
set "PYW="
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if defined PYW goto deps
for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if defined PYW goto deps
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if defined PYW goto deps

rem ===== 2. Python net - skachivaem i stavim sami =====
echo Python не найден. Скачиваю официальный установщик python.org (~26 МБ)...
set "PYINST=%TEMP%\python-3.12.10-setup.exe"
curl -f -L -# -o "%PYINST%" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
if errorlevel 1 goto manual

echo Устанавливаю Python - это займёт 1-2 минуты, ничего нажимать не нужно...
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0
if errorlevel 1 goto manual
del "%PYINST%" >nul 2>nul

set "PYW="
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW goto manual

rem ===== 3. Zavisimosti: Pillow dlya smailov 7TV =====
:deps
set "PYEXE=%PYW:pythonw.exe=python.exe%"
"%PYEXE%" -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo Ставлю пакет для смайлов 7TV...
  "%PYEXE%" -m pip install --quiet --disable-pip-version-check pillow
)

rem ===== 4. Zapusk =====
start "" "%PYW%" "%~dp0twitch_chat_overlay.pyw"
exit /b 0

:manual
echo.
echo Автоматическая установка не удалась (нет интернета или нет прав).
echo Установите Python вручную: https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH",
echo затем запустите этот файл ещё раз.
echo.
pause
exit /b 1
