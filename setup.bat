@echo off
chcp 65001 >nul
rem Create .venv and install dependencies. Pass a python.exe path to override the default.
setlocal
cd /d "%~dp0"
set "PY=%~1"
if "%PY%"=="" for %%P in (py.exe) do if not "%%~$PATH:P"=="" set "PY=py -3"
if "%PY%"=="" set "PY=python"
%PY% -m venv .venv || goto :fail
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt || goto :fail
echo.
echo 安装完成。双击 start.bat 启动（调试用 run.bat，可以看到日志窗口）。
exit /b 0
:fail
echo 安装失败，请检查 Python 是否可用：setup.bat C:\Path\To\python.exe
exit /b 1
