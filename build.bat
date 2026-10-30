@echo off
rem ------------------------------------------------------------------------
rem  E.P.G. - Entropy Password Generator: автономный сборщик PyInstaller.
rem    build.bat           обычная сборка в папку (dist\E.P.G. - Entropy Password Generator)
rem    build.bat onefile   сборка в один автономный .exe
rem    build.bat debug     сборка с открытой консолью (для отладки ошибок)
rem  Python берется из .venv / ..\.venv или из системы; можно задать через PG_PYTHON
rem ------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "APP_NAME=E.P.G. - Entropy Password Generator"

set "PYEXE="
set "PYARGS="
if defined PG_PYTHON set "PYEXE=%PG_PYTHON%"
if not defined PYEXE if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE if exist "..\.venv\Scripts\python.exe" set "PYEXE=..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "..\Scripts\python.exe" set "PYEXE=..\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo Python not found. Install Python 3 or set PG_PYTHON to the full path of python.exe
    pause
    exit /b 1
)

echo Using Python: %PYEXE% %PYARGS%

rem Проверка наличия PyInstaller (установит автоматически, если нет)
"%PYEXE%" %PYARGS% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    "%PYEXE%" %PYARGS% -m pip install pyinstaller
)

rem Разбор режимов запуска (по умолчанию: папка без консоли)
set "MODE_FLAG=--onedir"
set "CONSOLE_FLAG=--windowed"

if /i "%~1"=="onefile" set "MODE_FLAG=--onefile"
if /i "%~1"=="debug"   set "CONSOLE_FLAG=--console"

rem Автоподхват иконки, если файл icon.ico лежит в папке проекта
set "ICON_FLAG="
if exist "icon.ico" (
    set "ICON_FLAG=--icon icon.ico"
    echo Using icon: icon.ico
) else (
    echo Note: icon.ico not found, building with default icon.
)

echo.
echo Building "%APP_NAME%" (%MODE_FLAG% %CONSOLE_FLAG%)...

"%PYEXE%" %PYARGS% -m PyInstaller --noconfirm --clean ^
    %MODE_FLAG% %CONSOLE_FLAG% %ICON_FLAG% ^
    --name "%APP_NAME%" ^
    --add-data "entropy_gui.html;." ^
    gui_launcher.py

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo BUILD FAILED, code %RC%.
    pause
    exit /b %RC%
)

rem Страховка для режима --onedir: копируем HTML рядом с .exe
if "%MODE_FLAG%"=="--onedir" (
    if exist "entropy_gui.html" (
        copy /y "entropy_gui.html" "dist\%APP_NAME%\entropy_gui.html" >nul
    )
)

echo BUILD SUCCESSFUL! Result is in "dist\%APP_NAME%".
pause
exit /b 0