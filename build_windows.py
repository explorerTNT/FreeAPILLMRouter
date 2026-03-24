"""
Скрипт сборки .exe для Windows.
Запуск: python build_windows.py
Автоматически конвертирует icon.png в .ico перед сборкой.
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
icon_ico = project_dir / "icon.ico"
dist_dir = str(project_dir / "dist")
build_dir = str(project_dir / "build")
spec_dir = str(project_dir)

print(f"Папка проекта: {project_dir}")

# --- Конвертируем PNG → ICO ---
if not icon_png.exists():
    print(f"Ошибка: {icon_png} не найден!")
    print("Положи icon.png в папку проекта и запусти снова.")
    exit(1)

print("Конвертирую icon.png → icon.ico...")
img = Image.open(icon_png).convert("RGBA")
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(icon_ico, format="ICO", sizes=sizes)
print("Иконка готова!")

# --- Сборка ---
print("Начинаю сборку...")

PyInstaller.__main__.run([
    app_py,
    "--name=ApiFreeLLM-Proxy",
    "--onedir",
    "--noconsole",
    f"--icon={str(icon_ico)}",
    f"--add-data={main_py};.",
    f"--add-data={config_py};.",
    f"--add-data={str(icon_png)};.",
    f"--distpath={dist_dir}",
    f"--workpath={build_dir}",
    f"--specpath={spec_dir}",

    # === Все зависимости которые PyInstaller может пропустить ===

    # uvicorn и его подмодули
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

    # FastAPI и его зависимости
    "--hidden-import=fastapi",
    "--hidden-import=fastapi.applications",
    "--hidden-import=fastapi.routing",
    "--hidden-import=fastapi.responses",
    "--hidden-import=starlette",
    "--hidden-import=starlette.applications",
    "--hidden-import=starlette.routing",
    "--hidden-import=starlette.middleware",
    "--hidden-import=starlette.responses",
    "--hidden-import=starlette.requests",
    "--hidden-import=starlette.status",
    "--hidden-import=starlette.types",

    # HTTP клиент
    "--hidden-import=httpx",
    "--hidden-import=httpcore",
    "--hidden-import=h11",
    "--hidden-import=anyio",
    "--hidden-import=anyio._backends",
    "--hidden-import=anyio._backends._asyncio",
    "--hidden-import=sniffio",
    "--hidden-import=certifi",
    "--hidden-import=idna",

    # Pydantic (нужен FastAPI)
    "--hidden-import=pydantic",
    "--hidden-import=pydantic.fields",
    "--hidden-import=pydantic_core",
    "--hidden-import=annotated_types",
    "--hidden-import=typing_extensions",

    # Системный трей
    "--hidden-import=pystray",
    "--hidden-import=pystray._win32",
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image",
    "--hidden-import=PIL.ImageDraw",

    # Стандартные модули которые иногда пропускаются
    "--hidden-import=multiprocessing",
    "--hidden-import=asyncio",
    "--hidden-import=json",
    "--hidden-import=email.mime.text",

    # Собираем все пакеты целиком (надёжнее чем по модулям)
    "--collect-all=uvicorn",
    "--collect-all=fastapi",
    "--collect-all=starlette",
    "--collect-all=httpx",
    "--collect-all=httpcore",
    "--collect-all=pystray",
    "--collect-all=anyio",
])

# --- Убираем временный .ico ---
if icon_ico.exists():
    icon_ico.unlink()
    print("Временный icon.ico удалён.")

print()
print("=" * 50)
print("Сборка завершена!")
print(f"Файл: {project_dir / 'dist' / 'ApiFreeLLM-Proxy' / 'ApiFreeLLM-Proxy.exe'}")
print("=" * 50)
