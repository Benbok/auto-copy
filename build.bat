@echo off
REM Сборка AutoCopy.exe (одним файлом, без консольного окна)
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --onefile --windowed --name AutoCopy ^
  --hidden-import pystray._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import pynput.keyboard._win32 auto_copy.py
echo.
echo Готово: dist\AutoCopy.exe
pause
