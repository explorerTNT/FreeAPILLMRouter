"""
Скрипт сборки .app для macOS.
Запуск: python build_macos.py
Результат: папка dist/ApiFreeLLM-Proxy.app
"""

import os
from pathlib import Path
import PyInstaller.__main__

# Папка проекта — где лежит этот скрипт
project_dir = Path(__file__).parent.resolve()

# Переходим в папку проекта
os.chdir(project_dir)

# Абсолютные пути к файлам — чтобы PyInstaller точно их нашёл
app_py = str(project_dir / "app.py")
main_py = str(project_dir / "main.py")
config_py = str(project_dir / "config.py")

# Папка куда складывать результат
dist_dir = str(project_dir / "dist")
build_dir = str(project_dir / "build")
spec_dir = str(project_dir)

print(f"Папка проекта: {project_dir}")
print(f"app.py: {app_py}")
print(f"main.py: {main_py}")
print(f"config.py: {config_py}")
print("Начинаю сборку...")

PyInstaller.__main__.run([
    app_py,
    "--name=ApiFreeLLM-Proxy",
    "--onedir",
    "--noconsole",
    "--osx-bundle-identifier=com.apifreellm.proxy",
    f"--add-data={main_py}:.",
    f"--add-data={config_py}:.",
    f"--distpath={dist_dir}",
    f"--workpath={build_dir}",
    f"--specpath={spec_dir}",
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
print(f"Файл: {project_dir / 'dist' / 'ApiFreeLLM-Proxy'}")
print("=" * 50)
