@echo off
rem 一键打包：主程序 + 精简卸载器 + 安装包（产物在 dist\）
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller || goto :fail

echo [1/3] 打包主程序…
python -m PyInstaller --noconfirm --clean --onefile --noconsole --icon app.ico ^
    --version-file version_app.txt ^
    --name DeepSeekBalanceWidget ^
    --exclude-module numpy --exclude-module pandas --exclude-module matplotlib ^
    --exclude-module PIL ^
    main.py || goto :fail

echo [2/3] 打包卸载器…
python -m PyInstaller --noconfirm --clean --onefile --noconsole --icon app.ico ^
    --version-file version_app.txt ^
    --name DeepSeekWidget-Uninstall installer.py || goto :fail
if not exist uninstaller mkdir uninstaller
copy /y "dist\DeepSeekWidget-Uninstall.exe" "uninstaller\Uninstall.exe" >nul || goto :fail

echo [3/3] 打包安装包…
python -m PyInstaller --noconfirm --clean --onefile --noconsole --icon app.ico ^
    --version-file version_setup.txt ^
    --name DeepSeekWidget-Setup ^
    --add-data "dist\DeepSeekBalanceWidget.exe;payload" ^
    --add-data "uninstaller\Uninstall.exe;uninstaller" ^
    --add-data "app.ico;." ^
    installer.py || goto :fail

echo.
echo 完成：%~dp0dist\DeepSeekWidget-Setup.exe
pause
exit /b

:fail
echo.
echo 打包失败，把上面的报错发我。
pause
exit /b 1
