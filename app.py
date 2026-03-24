"""
Главная точка входа приложения.
Запускает прокси-сервер в фоне и показывает иконку в системном трее.

Пользователь видит:
- Иконку в трее (зелёная = работает)
- Правый клик → меню (скопировать адрес, открыть конфиг, выход)
- Всплывающие уведомления при ошибках (даже без терминала)
"""

import os
import sys
import threading
import webbrowser
import subprocess
import platform
import logging
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ====== GUI-УВЕДОМЛЕНИЯ (КРОСС-ПЛАТФОРМЕННЫЕ) ======

def show_message(title: str, text: str, is_error: bool = False):
    """
    Показывает всплывающее окно с сообщением.
    Работает на macOS, Windows и Linux без дополнительных библиотек.
    
    Это нужно чтобы пользователь видел ошибки даже без терминала —
    например, при запуске .app двойным кликом.
    """
    system = platform.system()
    logger.info("Показываю уведомление: %s — %s", title, text)

    try:
        if system == "Darwin":  # macOS
            # osascript — встроенный AppleScript, есть на каждом маке
            icon_type = "stop" if is_error else "note"
            script = (
                f'display dialog "{text}" '
                f'with title "{title}" '
                f'with icon {icon_type} '
                f'buttons {{"OK"}} '
                f'default button "OK"'
            )
            subprocess.run(["osascript", "-e", script], check=False)

        elif system == "Windows":
            # PowerShell с MessageBox — есть на каждой Windows
            msg_type = "16" if is_error else "64"  # 16=Error, 64=Info
            ps_script = (
                f'Add-Type -AssemblyName System.Windows.Forms; '
                f'[System.Windows.Forms.MessageBox]::Show('
                f'"{text}", "{title}", "OK", {msg_type})'
            )
            subprocess.run(
                ["powershell", "-Command", ps_script],
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        else:  # Linux
            # zenity — часто предустановлен на Linux с GUI
            msg_type = "--error" if is_error else "--info"
            subprocess.run(
                ["zenity", msg_type, "--title", title, "--text", text],
                check=False,
            )
    except Exception as e:
        # Если GUI недоступен — хотя бы в лог
        logger.error("Не удалось показать уведомление: %s", e)


def show_first_run_message(config_path: Path):
    """
    Показывает приветственное окно при первом запуске.
    Объясняет пользователю что делать.
    """
    text = (
        f"Добро пожаловать в ApiFreeLLM Proxy!\\n\\n"
        f"Создан файл конфигурации:\\n"
        f"{config_path}\\n\\n"
        f"Что нужно сделать:\\n"
        f"1. Сейчас откроется config.json\\n"
        f"2. Замените ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ на ваш API-ключ\\n"
        f"3. Сохраните файл и запустите приложение снова\\n\\n"
        f"Получить ключ: https://apifreellm.com"
    )
    show_message("ApiFreeLLM Proxy — Первый запуск", text)


def show_no_key_message(config_path: Path):
    """Показывает ошибку если ключ не заполнен."""
    text = (
        f"API-ключ не настроен!\\n\\n"
        f"Откройте файл:\\n"
        f"{config_path}\\n\\n"
        f"Замените ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ на ваш API-ключ\\n"
        f"и запустите приложение снова.\\n\\n"
        f"Получить ключ: https://apifreellm.com"
    )
    show_message("ApiFreeLLM Proxy — Ошибка", text, is_error=True)


def show_bad_json_message(config_path: Path):
    """Показывает ошибку если config.json повреждён."""
    text = (
        f"Файл config.json повреждён!\\n\\n"
        f"Путь: {config_path}\\n\\n"
        f"Удалите этот файл и запустите приложение снова —\\n"
        f"он создастся заново."
    )
    show_message("ApiFreeLLM Proxy — Ошибка", text, is_error=True)


def show_running_message(port: int):
    """Показывает уведомление что прокси запущен."""
    text = (
        f"Прокси запущен и работает!\\n\\n"
        f"Настройки для ChatboxAI:\\n"
        f"API Host: http://localhost:{port}/v1/chat/completions\\n"
        f"API Key: anything\\n"
        f"Model: apifreellm\\n\\n"
        f"Иконка в трее — правый клик для меню."
    )
    show_message("ApiFreeLLM Proxy", text)


# ====== ОСНОВНЫЕ ФУНКЦИИ ======

def get_base_dir() -> Path:
    """
    Возвращает директорию где лежит приложение.
    Работает и для python app.py, и для собранного .exe/.app.
    """
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable)
        # macOS .app: Contents/MacOS/App → поднимаемся до папки с .app
        if exe_path.parts[-2] == "MacOS" and exe_path.parts[-3] == "Contents":
            return exe_path.parent.parent.parent.parent
        else:
            return exe_path.parent
    else:
        return Path(__file__).parent


def show_config_file(config_path: Path):
    """Открывает config.json в стандартном редакторе ОС."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(config_path))
        elif system == "Darwin":
            subprocess.run(["open", str(config_path)])
        else:
            subprocess.run(["xdg-open", str(config_path)])
    except Exception as e:
        logger.error("Не удалось открыть конфиг: %s", e)


def ensure_config_exists():
    """
    Проверяет конфигурацию с GUI-уведомлениями.
    Возвращает config dict или None если не настроен.
    """
    base_dir = get_base_dir()
    os.chdir(base_dir)

    # Импортируем после chdir чтобы CONFIG_PATH был правильным
    from config import CONFIG_PATH, DEFAULT_CONFIG

    # --- Первый запуск: файла нет ---
    if not CONFIG_PATH.exists():
        logger.info("Первый запуск — создаю config.json")

        from config import create_default_config
        create_default_config()

        # Показываем GUI-окно с инструкцией
        show_first_run_message(CONFIG_PATH)

        # Открываем файл в редакторе
        show_config_file(CONFIG_PATH)
        return None

    # --- Читаем конфиг ---
    import json
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError:
        logger.error("config.json повреждён")
        show_bad_json_message(CONFIG_PATH)
        return None

    # --- Проверяем ключ ---
    api_key = config.get("api_key", "")
    if not api_key or api_key == DEFAULT_CONFIG["api_key"]:
        logger.error("API-ключ не настроен")
        show_no_key_message(CONFIG_PATH)
        show_config_file(CONFIG_PATH)
        return None

    # --- Дополняем недостающие поля ---
    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_value

    logger.info("Конфиг загружен, ключ настроен.")
    return config


def create_tray_icon(color: str = "green") -> Image.Image:
    """Создаёт иконку для трея программно."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    colors = {
        "green": (76, 175, 80),
        "red": (244, 67, 54),
        "yellow": (255, 193, 7),
    }
    fill_color = colors.get(color, colors["green"])

    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=fill_color,
    )

    try:
        draw.text((20, 12), "P", fill="white")
    except Exception:
        pass

    return image


def copy_to_clipboard(text: str):
    """Копирует текст в буфер обмена."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
        elif system == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=True,
            )
        logger.info("Скопировано: %s", text)
    except Exception as e:
        logger.error("Не удалось скопировать: %s", e)


def run_server(host: str, port: int):
    """Запускает FastAPI-сервер в отдельном потоке."""
    from main import app
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    """Главная функция — запускает сервер и трей."""
    import pystray

    logger.info("Запуск ApiFreeLLM Proxy...")

    # --- Проверяем конфиг (с GUI-уведомлениями) ---
    config = ensure_config_exists()
    if config is None:
        logger.info("Конфиг не настроен — завершаю работу.")
        return

    host = config["server"]["host"]
    port = config["server"]["port"]
    proxy_url = f"http://localhost:{port}/v1"

    # --- Запускаем сервер в фоне ---
    server_thread = threading.Thread(
        target=run_server,
        args=(host, port),
        daemon=True,
    )
    server_thread.start()

    logger.info("Сервер запущен в фоне!")
    logger.info("Адрес: %s", proxy_url)

    # --- Показываем уведомление что прокси работает ---
    # Запускаем в отдельном потоке чтобы не блокировать трей
    threading.Thread(
        target=show_running_message,
        args=(port,),
        daemon=True,
    ).start()

    # --- Меню трея ---
    def on_copy_url(icon, item):
        copy_to_clipboard(proxy_url)

    def on_copy_endpoint(icon, item):
        copy_to_clipboard(f"{proxy_url}/chat/completions")

    def on_open_config(icon, item):
        from config import CONFIG_PATH
        show_config_file(CONFIG_PATH)

    def on_open_docs(icon, item):
        webbrowser.open(f"http://localhost:{port}/docs")

    def on_quit(icon, item):
        logger.info("Завершение работы...")
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            f"✅ Прокси работает (порт {port})",
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📋 Копировать API URL", on_copy_url),
        pystray.MenuItem("📋 Копировать полный эндпоинт", on_copy_endpoint),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("⚙️ Открыть config.json", on_open_config),
        pystray.MenuItem("📖 Документация API", on_open_docs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Выход", on_quit),
    )

    icon = pystray.Icon(
        name="ApiFreeLLM Proxy",
        icon=create_tray_icon("green"),
        title=f"ApiFreeLLM Proxy — localhost:{port}",
        menu=menu,
    )

    logger.info("Иконка добавлена в системный трей.")
    icon.run()


if __name__ == "__main__":
    main()
