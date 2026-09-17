@echo off
rem 静默启动挂件（不弹黑框）
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0main.py"
    exit /b
)

where pyw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pyw.exe "%~dp0main.py"
    exit /b
)

echo 没找到 pythonw.exe，请先安装 Python 3.10+ 并勾选 Add to PATH。
pause
