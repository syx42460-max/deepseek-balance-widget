@echo off
rem 打包成单文件 exe，产物在 dist\DeepSeekBalanceWidget.exe
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller || goto :fail
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --icon app.ico --version-file version_app.txt ^
    --name DeepSeekBalanceWidget ^
    --exclude-module numpy --exclude-module pandas --exclude-module matplotlib ^
    --exclude-module PIL --exclude-module tkinter.test ^
    main.py || goto :fail

echo.
echo 打包完成：%~dp0dist\DeepSeekBalanceWidget.exe
pause
exit /b

:fail
echo.
echo 打包失败，请把上面的报错发我。
pause
exit /b 1
