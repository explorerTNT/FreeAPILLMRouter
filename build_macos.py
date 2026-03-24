"""
Скрипт сборки .app для macOS.
Запуск: python build_macos.py
Автоматически конвертирует icon.png в .icns перед сборкой.
"""

import os
from pathlib import Path
from PIL import Image
import PyInstaller.__main__

project_dir = Path(__file__).parent.resolve()
os.chdir(project_dir)

# Пути к файлам
app_py = str(project_dir / "app.py")
main_py = str(project_dir / "main.py")
config_py = str(project_dir / "config.py")
icon_png = project_dir / "icon.png"
icon_icns = project_dir / "icon.icns"
dist_dir = str(project_dir / "dist")
build_dir = str(project_dir / "build")
spec_dir = str(project_dir)

print(f"Папка проекта: {project_dir}")

# --- Конвертируем PNG → ICNS ---
if not icon_png.exists():
    print(f"Ошибка: {icon_png} не найден!")
    print("Положи icon.png в папку проекта и запусти снова.")
    exit(1)

print("Конвертирую icon.png → icon.icns...")
img = Image.open(icon_png).convert("RGBA")
img.save(icon_icns, format="ICNS")
print("Иконка готова!")

# --- Сборка ---
print("Начинаю сборку...")

PyInstaller.__main__.run([
    app_py,
    "--name=ApiFreeLLM-Proxy",
    "--onedir",
    "--noconsole",
    "--osx-bundle-identifier=com.apifreellm.proxy",
    f"--icon={str(icon_icns)}",
    f"--add-data={main_py}:.",
    f"--add-data={config_py}:.",
    f"--add-data={str(icon_png)}:.",
    f"--distpath={dist_dir}",
    f"--workpath={build_dir}",
    f"--specpath={spec_dir}",

    # === Все зависимости ===
    "--hidden-import=uvicorn",
    "--hidden-import=uvicorn.main",
    "--hidden-import=uvicorn.config",
    "--hidden-import=uvicorn.server",
    "--hidden-import=uvicorn.logging",
    "--hidden-import=uvicorn.loops",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=uvicorn.loops.asyncio",
    "--hidden-import=uvicorn.protocols",
    "--hidden-import=uvicorn.protocols.http",
    "--hidden-import=uvicorn.protocols.http.auto",
    "--hidden-import=uvicorn.protocols.http.h11_impl",
    "--hidden-import=uvicorn.protocols.http.httptools_impl",
    "--hidden-import=uvicorn.protocols.websockets",
    "--hidden-import=uvicorn.protocols.websockets.auto",
    "--hidden-import=uvicorn.lifespan",
    "--hidden-import=uvicorn.lifespan.on",
    "--hidden-import=uvicorn.lifespan.off",
    "--hidden-import=fastapi",
    "--hidden-import=starlette",
    "--hidden-import=httpx",
    "--hidden-import=httpcore",
    "--hidden-import=h11",
    "--hidden-import=anyio",
    "--hidden-import=anyio._backends",
    "--hidden-import=anyio._backends._asyncio",
    "--hidden-import=sniffio",
    "--hidden-import=certifi",
    "--hidden-import=pydantic",
    "--hidden-import=pydantic_core",
    "--hidden-import=pystray",
    "--hidden-import=pystray._darwin",

    "--collect-all=uvicorn",
    "--collect-all=fastapi",
    "--collect-all=starlette",
    "--collect-all=httpx",
    "--collect-all=httpcore",
    "--collect-all=pystray",
    "--collect-all=anyio",
])

# --- Убираем временный .icns ---
if icon_icns.exists():
    icon_icns.unlink()
    print("Временный icon.icns удалён.")

print()
print("=" * 50)
print("Сборка завершена!")
print(f"Файл: {project_dir / 'dist' / 'ApiFreeLLM-Proxy.app'}")
print("=" * 50)
