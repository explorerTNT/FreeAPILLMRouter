# 🤖 ApiFreeLLM Proxy

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)

> OpenAI-совместимый прокси-сервер для ApiFreeLLM с умной генерацией названий чатов

**ApiFreeLLM Proxy** — это удобный прокси-сервер, который позволяет использовать сервис [ApiFreeLLM](https://apifreellm.com) через стандартный OpenAI API интерфейс. Идеально подходит для интеграции с приложениями вроде ChatboxAI, CCR и другими клиентами, поддерживающими OpenAI API.

## ✨ Возможности

### 🚀 Основные функции
- **OpenAI-совместимый API** — полная совместимость с `/v1/chat/completions`
- **Автоматическое управление rate limit** — 1 запрос каждые 25 секунд
- **Умная генерация названий чатов** — мгновенно, без дополнительных API вызовов
- **Потоковая передача (SSE)** — поддержка `stream: true` для плавного вывода текста
- **Мультимодальный контент** — поддержка изображений и структурированного текста

### 🖥️ Пользовательский интерфейс
- **Системный трей** — минималистичный интерфейс без отвлекающих окон
- **Автозапуск сервера** — приложение запускает прокси автоматически
- **Копирование настроек** — быстрый доступ к API URL и настройкам
- **Документация API** — встроенный Swagger UI для тестирования

### 🔧 Технические особенности
- **Обработка ошибок** — автоматические повторы при rate limit (429)
- **Подробное логирование** — ротация логов (макс. 5 МБ, 4 файла)
- **Кроссплатформенность** — поддержка Windows, macOS и Linux
- **Безопасность** — валидация запросов и фильтрация мусорных данных

## 📦 Установка

### Предварительные требования
- Python 3.8 или выше
- Аккаунт на [ApiFreeLLM](https://apifreellm.com) с API ключом

### Установка из исходников

1. **Клонируйте репозиторий:**
   ```bash
   git clone https://github.com/your-username/FreeAPILLMRouter.git
   cd FreeAPILLMRouter
   ```

2. **Установите зависимости:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Запустите приложение:**
   ```bash
   python app.py
   ```

4. **Настройте API ключ:**
   - При первом запуске автоматически создастся `config.json`
   - Откройте файл и замените `"ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ"` на ваш реальный API ключ
   - Перезапустите приложение

### Скачивание готовых бинарных файлов

Готовые сборки доступны в разделе [Releases](https://github.com/your-username/FreeAPILLMRouter/releases):

- **Windows**: `ApiFreeLLM-Proxy.exe` — просто запустите и следуйте инструкциям
- **macOS**: `ApiFreeLLM-Proxy.app` — перетащите в Applications
- **Linux**: Запустите через Python (сборка для Linux не предоставляется)

## ⚙️ Настройка

### config.json

```json
{
  "api_key": "ваш_реальный_api_ключ_здесь",
  "api_endpoint": "https://apifreellm.com/api/v1/chat",
  "model": "apifreellm",
  "server": {
    "host": "0.0.0.0",
    "port": 8000
  },
  "upstream_timeout_seconds": 60
}
```

### Переменные конфигурации

| Параметр | Описание | Значение по умолчанию |
|----------|----------|----------------------|
| `api_key` | Ваш API ключ от ApiFreeLLM | Требуется настройка |
| `api_endpoint` | URL API ApiFreeLLM | `https://apifreellm.com/api/v1/chat` |
| `model` | Название модели для API | `apifreellm` |
| `server.host` | Хост для прослушивания | `0.0.0.0` |
| `server.port` | Порт сервера | `8000` |
| `upstream_timeout_seconds` | Таймаут запросов к ApiFreeLLM | `60` |

## 🔌 Использование

### Настройка ChatboxAI

1. Откройте настройки ChatboxAI
2. Выберите "Custom" в качестве провайдера
3. Введите следующие параметры:
   - **API Host**: `http://localhost:8000/v1/chat/completions`
   - **API Key**: `anything` (любой текст)
   - **Model**: `apifreellm`

### Настройка других клиентов

Для любого OpenAI-совместимого клиента используйте:
- **Base URL**: `http://localhost:8000/v1`
- **API Key**: любой текст (не используется)
- **Model**: `apifreellm`

### Проверка работы

1. Запустите приложение
2. Иконка появится в системном трее
3. Правой кнопкой кликните на иконку → "Документация API"
4. Или перейдите по адресу: `http://localhost:8000/docs`

## 🏗️ Сборка из исходников

### Windows
```bash
python build_windows.py
```
Результат: `dist\ApiFreeLLM-Proxy\ApiFreeLLM-Proxy.exe`

### macOS
```bash
python build_macos.py
```
Результат: `dist/ApiFreeLLM-Proxy.app`

## 📋 API Эндпоинты

### POST `/v1/chat/completions`
Основной эндпоинт для чата. Полностью совместим с OpenAI API.

**Пример запроса:**
```json
{
  "messages": [
    {"role": "user", "content": "Привет!"}
  ],
  "model": "apifreellm",
  "stream": true
}
```

**Пример ответа:**
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1677858242,
  "model": "apifreellm",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Привет! Как я могу помочь?"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### GET `/v1/models`
Возвращает список доступных моделей.

### GET `/health`
Проверка работоспособности сервера.

## 🛠️ Устранение неисправностей

### Проблемы с запуском

1. **"API-ключ не настроен"**
   - Откройте `config.json`
   - Замените `"ВСТАВЬТЕ_СЮДА_ВАШ_КЛЮЧ"` на ваш API ключ

2. **"config.json содержит невалидный JSON"**
   - Проверьте синтаксис JSON в файле
   - Или удалите файл и перезапустите приложение

3. **Сервер не запускается**
   - Проверьте, что порт 8000 свободен
   - Измените порт в `config.json` если нужно

### Проблемы с API

1. **429 Rate Limit**
   - Сервер автоматически обрабатывает rate limit
   - Подождите или уменьшите частоту запросов

2. **Таймауты**
   - Проверьте интернет-соединение
   - Увеличьте `upstream_timeout_seconds` в конфиге

3. **Ошибки в логах**
   - Логи находятся в `proxy.log`
   - Ищите подробную информацию там

## 📁 Структура проекта

```
FreeAPILLMRouter/
├── app.py                 # Главное GUI приложение с треем
├── main.py               # FastAPI сервер и прокси-логика
├── config.py             # Модуль конфигурации
├── requirements.txt      # Зависимости Python
├── build_windows.py      # Скрипт сборки для Windows
├── build_macos.py        # Скрипт сборки для macOS
├── icon.png              # Иконка приложения
├── README.md            # Эта документация
├── LICENSE              # Лицензия MIT
└── .gitignore           # Git ignore правила
```

## 🔍 Логика работы

### Генерация названий чатов
Приложение автоматически распознаёт запросы на генерацию названий чатов и создаёт их локально без использования API:

1. **Распознавание**: Анализ последнего сообщения на ключевые фразы
2. **Извлечение**: Поиск релевантного текста из истории сообщений
3. **Форматирование**: Обрезка и очистка текста до красивого названия

### Управление rate limit
- **Очередь запросов**: Гарантирует интервал минимум 25 секунд между запросами
- **Блокировка**: Использует asyncio.Lock для потокобезопасности
- **Повторы**: Автоматическая обработка ошибок 429 с экспоненциальной задержкой

### Мультимодальный контент
Поддерживает различные форматы контента:
- **Строка**: `"Привет, мир!"`
- **Массив**: `[{"type": "text", "text": "Привет"}, {"type": "image_url", {"url": "..."}}]`

## 🤝 Вклад в проект

Мы приветствуем вклад в развитие проекта!

1. Fork репозиторий
2. Создайте feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit изменения (`git commit -m 'Add some AmazingFeature'`)
4. Push в branch (`git push origin feature/AmazingFeature`)
5. Откройте Pull Request

## 📄 Лицензия

Проект распространяется под лицензией MIT. Подробности в файле [LICENSE](LICENSE).

## 🙏 Благодарности

- [ApiFreeLLM](https://apifreellm.com) — за предоставление API
- [FastAPI](https://fastapi.tiangolo.com/) — за отличный веб-фреймворк
- [pystray](https://github.com/moses-palmer/pystray) — за системный трей

## 📞 Поддержка

Если у вас возникли проблемы или вопросы:

1. Проверьте [Issues](https://github.com/your-username/FreeAPILLMRouter/issues) на GitHub
2. Создайте новый issue с подробным описанием проблемы
3. Прикрепите логи из `proxy.log` если возможно

---

**ApiFreeLLM Proxy** — ваш мост к мощному ИИ через удобный интерфейс! 🚀