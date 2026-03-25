"""
Главная точка входа приложения.
Запускает прокси-сервер в фоне и показывает иконку в системном трее.
"""

import os
import sys
import threading
import webbrowser
import subprocess
import platform
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# === Защита от отсутствия консоли (Windows + PyInstaller --noconsole) ===
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn
from PIL import Image, ImageDraw


# ====== ЛОГИРОВАНИЕ С АВТО-РОТАЦИЕЙ ======

def get_base_dir() -> Path:
    """Возвращает директорию где лежит приложение."""
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable)
        if exe_path.parts[-2] == "MacOS" and exe_path.parts[-3] == "Contents":
            return exe_path.parent.parent.parent.parent
        else:
            return exe_path.parent
    else:
        return Path(__file__).parent


LOG_FILE = get_base_dir() / "proxy.log"

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)

console_handler = logging.StreamHandler()

log_format = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(log_format)
console_handler.setFormatter(log_format)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)


def handle_exception(exc_type, exc_value, exc_traceback):
    """Перехватывает любые необработанные исключения."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical(
        "Необработанная ошибка!",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


sys.excepthook = handle_exception

logger.info("=" * 50)
logger.info("Запуск приложения...")
logger.info("Лог-файл: %s", LOG_FILE)
logger.info("Макс. размер лога: 5 МБ × 4 файла")
logger.info("Платформа: %s", platform.system())
logger.info("Python: %s", sys.version)
logger.info("Frozen: %s", getattr(sys, "frozen", False))
logger.info("Base dir: %s", get_base_dir())
logger.info("=" * 50)


# ====== GUI-УВЕДОМЛЕНИЯ ======

def show_message(title: str, text: str, is_error: bool = False):
    """Показывает всплывающее окно с сообщением."""
    system = platform.system()
    logger.info("Уведомление: %s", title)

    try:
        if system == "Darwin":
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
            msg_type = "16" if is_error else "64"
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

        else:
            msg_type = "--error" if is_error else "--info"
            subprocess.run(
                ["zenity", msg_type, "--title", title, "--text", text],
                check=False,
            )
    except Exception as e:
        logger.error("Не удалось показать уведомление: %s", e)


def show_first_run_message(config_path: Path):
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
    text = (
        f"Файл config.json повреждён!\\n\\n"
        f"Путь: {config_path}\\n\\n"
        f"Удалите этот файл и запустите приложение снова —\\n"
        f"он создастся заново."
    )
    show_message("ApiFreeLLM Proxy — Ошибка", text, is_error=True)


def show_running_message(port: int):
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

    [ИЗМЕНЕНО] Теперь ловит ConfigError из load_config()
    вместо того чтобы дублировать логику проверок.
    load_config() бросает исключение — мы его ловим,
    определяем тип ошибки и показываем нужное GUI-окно.
    """
    base_dir = get_base_dir()
    os.chdir(base_dir)
    logger.info("Рабочая директория: %s", base_dir)

    from config import CONFIG_PATH, ConfigError

    logger.info("Путь к конфигу: %s", CONFIG_PATH)

    try:
        from config import load_config
        config = load_config()
    except ConfigError as e:
        error_msg = str(e)
        logger.info("Ошибка конфигурации: %s", error_msg)

        # Определяем тип ошибки по тексту и показываем нужное окно
        if "Создан config.json" in error_msg:
            # Первый запуск — файл только что создан
            logger.info("Первый запуск — создаю config.json")
            show_first_run_message(CONFIG_PATH)
            show_config_file(CONFIG_PATH)
        elif "невалидный JSON" in error_msg:
            # Файл повреждён
            logger.error("config.json повреждён")
            show_bad_json_message(CONFIG_PATH)
        elif "не настроен" in error_msg:
            # Ключ не заполнен
            logger.error("API-ключ не настроен")
            show_no_key_message(CONFIG_PATH)
            show_config_file(CONFIG_PATH)
        else:
            # Неизвестная ошибка конфигурации
            show_message(
                "ApiFreeLLM Proxy — Ошибка",
                f"Ошибка конфигурации:\\n{error_msg}",
                is_error=True,
            )
        return None

    logger.info("Конфиг загружен, ключ настроен.")
    return config


def create_tray_icon(color: str = "green") -> Image.Image:
    """Загружает или рисует иконку для трея."""
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys._MEIPASS)
        else:
            base = Path(__file__).parent

        icon_path = base / "icon.png"
        if icon_path.exists():
            img = Image.open(icon_path)
            img = img.resize((64, 64), Image.LANCZOS)
            return img
    except Exception as e:
        logger.warning("Не удалось загрузить icon.png: %s", e)

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
    try:
        logger.info("Запускаю uvicorn на %s:%d...", host, port)
        from main import app

        # log_config=None — отключаем встроенное логирование uvicorn,
        # оно падает без консоли (sys.stdout = None при --noconsole).
        # Наш собственный логгер (RotatingFileHandler) уже работает.
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            log_config=None,
        )
    except Exception as e:
        logger.critical("Сервер упал с ошибкой: %s", e, exc_info=True)
        show_message(
            "ApiFreeLLM Proxy — Ошибка",
            f"Сервер не смог запуститься:\\n{e}",
            is_error=True,
        )


def main():
    """Главная функция — запускает сервер и трей."""
    try:
        import pystray
    except Exception as e:
        logger.critical("Не удалось импортировать pystray: %s", e, exc_info=True)
        show_message("Ошибка", f"Не удалось загрузить модуль трея:\\n{e}", is_error=True)
        return

    logger.info("Запуск ApiFreeLLM Proxy...")

    config = ensure_config_exists()
    if config is None:
        logger.info("Конфиг не настроен — завершаю работу.")
        return

    host = config["server"]["host"]
    port = config["server"]["port"]
    proxy_url = f"http://localhost:{port}/v1"

    logger.info("Конфиг: host=%s, port=%d", host, port)

    server_thread = threading.Thread(
        target=run_server,
        args=(host, port),
        daemon=True,
    )
    server_thread.start()

    logger.info("Сервер запущен в фоне!")

    threading.Thread(
        target=show_running_message,
        args=(port,),
        daemon=True,
    ).start()

    def on_copy_url(icon, item):
        copy_to_clipboard(proxy_url)

    def on_copy_endpoint(icon, item):
        copy_to_clipboard(f"{proxy_url}/chat/completions")

    def on_open_config(icon, item):
        from config import CONFIG_PATH
        show_config_file(CONFIG_PATH)

    def on_open_docs(icon, item):
        webbrowser.open(f"http://localhost:{port}/docs")

    def on_open_log(icon, item):
        show_config_file(LOG_FILE)

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
        pystray.MenuItem("📄 Открыть логи", on_open_log),
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

    logger.info("Иконка добавлена в трей.")
    icon.run()


if __name__ == "__main__":
    main()
