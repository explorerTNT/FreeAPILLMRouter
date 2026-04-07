"""
Модуль для работы с конфигурацией.
При первом запуске создаёт config.json с настройками по умолчанию.
Пользователь вставляет туда свой API-ключ, после чего сервер запускается.

[ИЗМЕНЕНО] load_config теперь бросает ConfigError вместо sys.exit().
Это правильнее для серверного кода — вызывающий код (app.py)
сам решает: показать GUI-ошибку или просто упасть.
"""

import json
import sys
from pathlib import Path


class ConfigError(Exception):
    """
    Ошибка конфигурации.
    Бросается когда конфиг отсутствует, повреждён или не заполнен.
    Вызывающий код должен поймать это и показать пользователю
    понятное сообщение (GUI, консоль и т.д.).
    """
    pass


def get_config_dir() -> Path:
    """
    Определяет папку для config.json.

    - Если запущено как .app/.exe (PyInstaller) — рядом с приложением
    - Если запущено как python скрипт — рядом со скриптом

    Это важно потому что PyInstaller распаковывает файлы во временную папку,
    а нам нужен config.json в постоянном и удобном месте.
    """
    if getattr(sys, "frozen", False):
        # Собранное приложение (PyInstaller)
        exe_path = Path(sys.executable)

        # На macOS .app бандл: executable лежит в Contents/MacOS/
        # Нужно подняться до папки где лежит сам .app
        if exe_path.parts[-2] == "MacOS" and exe_path.parts[-3] == "Contents":
            # /path/to/dist/App.app/Contents/MacOS/App → /path/to/dist/
            return exe_path.parent.parent.parent.parent
        else:
            # Windows/Linux — .exe лежит прямо в папке
            return exe_path.parent
    else:
        # Обычный запуск через python — рядом со скриптом
        return Path(__file__).parent


# Файл конфига в удобном месте
CONFIG_PATH = get_config_dir() / "config.json"

# Шаблон конфига — создаётся при первом запуске
DEFAULT_CONFIG = {
    "api_key": "ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ",
    "api_endpoint": "https://apifreellm.com/api/v1/chat",
    "model": "apifreellm",
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
    },
    "upstream_timeout_seconds": 180,
}


def create_default_config() -> None:
    """Создаёт конфиг-файл с настройками по умолчанию."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)


def load_config() -> dict:
    """
    Загружает конфигурацию из config.json.

    Если файла нет — создаёт его и бросает ConfigError.
    Если ключ не заполнен — бросает ConfigError.
    Если JSON повреждён — бросает ConfigError.

    [ИЗМЕНЕНО] Раньше вызывал sys.exit() — это убивало процесс
    без возможности показать GUI-ошибку. Теперь бросает исключение,
    а app.py сам решает что делать (показать окно, залогировать и т.д.).

    Raises:
        ConfigError: если конфиг отсутствует, повреждён или не заполнен.

    Returns:
        dict: валидная конфигурация со всеми обязательными полями.
    """
    # --- Первый запуск: файла нет ---
    if not CONFIG_PATH.exists():
        create_default_config()
        raise ConfigError(
            f"Создан config.json: {CONFIG_PATH.resolve()}. "
            f"Заполните API-ключ и перезапустите приложение."
        )

    # --- Файл есть — читаем ---
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"config.json содержит невалидный JSON: {e}. "
            f"Исправьте файл или удалите его — он создастся заново."
        )

    # --- Проверяем что ключ заполнен ---
    api_key = config.get("api_key", "")

    if not api_key or api_key == DEFAULT_CONFIG["api_key"]:
        raise ConfigError(
            f"API-ключ не настроен в config.json ({CONFIG_PATH.resolve()}). "
            f"Замените ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ на свой ключ от apifreellm.com."
        )

    # --- Проверяем что все нужные поля на месте ---
    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_value
            # Информируем но не падаем — значение по умолчанию сработает
            print(
                f"  Предупреждение: поле '{key}' отсутствовало в конфиге, "
                f"использую значение по умолчанию: {default_value}"
            )

    return config
