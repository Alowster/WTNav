@echo off
REM Compila WTNav.exe en Windows. Requiere Python 3.10+ en el PATH.
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name WTNav --icon wtnav.ico run.py
echo.
echo Listo: dist\WTNav.exe
pause
