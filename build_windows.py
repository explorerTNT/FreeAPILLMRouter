"""
Скрипт сборки .exe для Windows.
Запуск: python build_windows.py
Результат: папка dist/ApiFreeLLM-Proxy/ApiFreeLLM-Proxy.exe
"""

import os
from pathlib import Path
import PyInstaller.__main__

# Переходим в папку где лежит этот скрипт
project_dir = Path(__file__).parent
os.chdir(project_dir)

print(f"Папка проекта: {project_dir}")
print("Начинаю сборку...")

PyInstaller.__main__.run([
    "app.py",
    "--name=ApiFreeLLM-Proxy",
    "--onedir",
    "--noconsole",
    "--icon=NONE",
    "--add-data=main.py;.",
    "--add-data=config.py;.",
    "--hidden-import=uvicorn.logging",
    "--hidden-import=uvicorn.loops",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=uvicorn.protocols",
    "--hidden-import=uvicorn.protocols.http",
    "--hidden-import=uvicorn.protocols.http.auto",
    "--hidden-import=uvicorn.protocols.websockets",
    "--hidden-import=uvicorn.protocols.websockets.auto",
    "--hidden-import=uvicorn.lifespan",
    "--hidden-import=uvicorn.lifespan.on",
])

print()
print("=" * 50)
print("Сборка завершена!")
print(f"Файл: {project_dir / 'dist' / 'ApiFreeLLM-Proxy' / 'ApiFreeLLM-Proxy.exe'}")
print("=" * 50)
