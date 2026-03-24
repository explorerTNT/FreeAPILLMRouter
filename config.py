"""
Модуль для работы с конфигурацией.
При первом запуске создаёт config.json с настройками по умолчанию.
Пользователь вставляет туда свой API-ключ, после чего сервер запускается.
"""

import json
import sys
from pathlib import Path


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
    "upstream_timeout_seconds": 60,
}


def create_default_config() -> None:
    """Создаёт конфиг-файл с настройками по умолчанию."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)


def load_config() -> dict:
    """
    Загружает конфигурацию из config.json.
    
    Если файла нет — создаёт его и просит пользователя заполнить.
    Если ключ не заполнен — выводит подсказку и завершает программу.
    """
    # --- Первый запуск: файла нет ---
    if not CONFIG_PATH.exists():
        print("=" * 55)
        print("  Первый запуск! Создаю файл конфигурации...")
        print("=" * 55)
        create_default_config()
        print()
        print(f"  Файл создан: {CONFIG_PATH.resolve()}")
        print()
        print("  Что нужно сделать:")
        print("  1. Открой файл config.json")
        print('  2. Замени "ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ" на свой API-ключ')
        print("  3. Запусти программу снова")
        print()
        print("  Пример ключа: apf_1234567898765432345678")
        print("=" * 55)
        sys.exit(0)

    # --- Файл есть — читаем ---
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Ошибка: config.json содержит невалидный JSON!")
        print(f"Детали: {e}")
        print(f"Исправь файл или удали его — он создастся заново.")
        sys.exit(1)

    # --- Проверяем что ключ заполнен ---
    api_key = config.get("api_key", "")

    if not api_key or api_key == DEFAULT_CONFIG["api_key"]:
        print("=" * 55)
        print("  Ошибка: API-ключ не настроен!")
        print("=" * 55)
        print()
        print(f"  Открой файл: {CONFIG_PATH.resolve()}")
        print('  Замени "ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ" на свой API-ключ')
        print()
        print("  Получить ключ: https://apifreellm.com")
        print("=" * 55)
        sys.exit(1)

    # --- Проверяем что все нужные поля на месте ---
    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_value
            print(f"  Предупреждение: поле '{key}' отсутствовало в конфиге, "
                  f"использую значение по умолчанию: {default_value}")

    return config
