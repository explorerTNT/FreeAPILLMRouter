"""
Скрипт сборки .exe для Windows.
Запуск: python build_windows.py
Автоматически конвертирует icon.png в .ico перед сборкой.
"""

import os
import sys
import traceback
from pathlib import Path

# Перенаправляем все ошибки в файл — чтобы видеть их даже если окно закрывается
log_file = Path(__file__).parent / "build_log.txt"
sys.stdout = open(log_file, "w", encoding="utf-8")
sys.stderr = sys.stdout

print("=== НАЧАЛО СБОРКИ ===")
print(f"Python: {sys.version}")
print(f"Папка: {Path(__file__).parent}")

try:
    from PIL import Image
    print("PIL: OK")
except Exception as e:
    print(f"PIL ОШИБКА: {e}")

try:
    import PyInstaller.__main__
    print("PyInstaller: OK")
except Exception as e:
    print(f"PyInstaller ОШИБКА: {e}")

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

print(f"\nФайлы проекта:")
print(f"  app.py: {Path(app_py).exists()}")
print(f"  main.py: {Path(main_py).exists()}")
print(f"  config.py: {Path(config_py).exists()}")
print(f"  icon.png: {icon_png.exists()}")

try:
    # --- Конвертируем PNG → ICO ---
    if not icon_png.exists():
        print(f"Ошибка: {icon_png} не найден!")
        sys.exit(1)

    print("\nКонвертирую icon.png -> icon.ico...")
    img = Image.open(icon_png).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(icon_ico, format="ICO", sizes=sizes)
    print("Иконка готова!")

    # --- Сборка ---
    print("\nНачинаю сборку PyInstaller...")

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
        "--hidden-import=httpx",
        "--hidden-import=httpcore",
        "--hidden-import=h11",
        "--hidden-import=anyio",
        "--hidden-import=anyio._backends",
        "--hidden-import=anyio._backends._asyncio",
        "--hidden-import=sniffio",
        "--hidden-import=certifi",
        "--hidden-import=idna",
        "--hidden-import=pydantic",
        "--hidden-import=pydantic.fields",
        "--hidden-import=pydantic_core",
        "--hidden-import=annotated_types",
        "--hidden-import=typing_extensions",
        "--hidden-import=pystray",
        "--hidden-import=pystray._win32",
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageDraw",
        "--hidden-import=multiprocessing",
        "--hidden-import=asyncio",
        "--hidden-import=json",
        "--hidden-import=re",
        "--hidden-import=email.mime.text",
        "--collect-all=uvicorn",
        "--collect-all=fastapi",
        "--collect-all=starlette",
        "--collect-all=httpx",
        "--collect-all=httpcore",
        "--collect-all=pystray",
        "--collect-all=anyio",
    ])

    print("\n=== СБОРКА ЗАВЕРШЕНА УСПЕШНО ===")

except Exception as e:
    print(f"\n=== ОШИБКА ===")
    print(f"{e}")
    print("\nПолный traceback:")
    traceback.print_exc()

finally:
    # Убираем временный .ico
    if icon_ico.exists():
        icon_ico.unlink()

    # Закрываем лог файл
    sys.stdout.close()

    # Выводим сообщение в реальную консоль
    sys.__stdout__.write(f"\nЛог сборки сохранён в: {log_file}\n")
    sys.__stdout__.write("Открой этот файл чтобы увидеть результат.\n")
    sys.__stdout__.flush()
