"""
Скрипт сборки .app для macOS.
Запуск: python build_macos.py
Автоматически конвертирует icon.png в .icns перед сборкой.
"""

import os
import sys
import traceback
from pathlib import Path

IS_CI = os.environ.get("CI", "false").lower() == "true"

print("=== НАЧАЛО СБОРКИ macOS ===")
print(f"Python: {sys.version}")
print(f"CI: {IS_CI}")

try:
    from PIL import Image
    print("PIL: OK")
except Exception as e:
    print(f"PIL ОШИБКА: {e}")
    sys.exit(1)

try:
    import PyInstaller.__main__
    print("PyInstaller: OK")
except Exception as e:
    print(f"PyInstaller ОШИБКА: {e}")
    sys.exit(1)

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

print(f"\nПапка проекта: {project_dir}")
print(f"\nФайлы проекта:")
print(f"  app.py: {Path(app_py).exists()}")
print(f"  main.py: {Path(main_py).exists()}")
print(f"  config.py: {Path(config_py).exists()}")
print(f"  icon.png: {icon_png.exists()}")

try:
    # --- Конвертируем PNG → ICNS ---
    if not icon_png.exists():
        print(f"Ошибка: {icon_png} не найден!")
        print("Положи icon.png в папку проекта и запусти снова.")
        sys.exit(1)

    print("\nКонвертирую icon.png → icon.icns...")
    img = Image.open(icon_png).convert("RGBA")
    img.save(icon_icns, format="ICNS")
    print("Иконка готова!")

    # --- Сборка ---
    print("\nНачинаю сборку PyInstaller...")

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

    print()
    print("=" * 50)
    print("=== СБОРКА ЗАВЕРШЕНА УСПЕШНО ===")
    print(f"Файл: {project_dir / 'dist' / 'ApiFreeLLM-Proxy.app'}")
    print("=" * 50)

except Exception as e:
    print(f"\n=== ОШИБКА ===")
    print(f"{e}")
    print("\nПолный traceback:")
    traceback.print_exc()
    sys.exit(1)

finally:
    if icon_icns.exists():
        icon_icns.unlink()
        print("Временный icon.icns удалён.")
