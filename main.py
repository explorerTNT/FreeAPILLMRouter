"""
Прокси-сервер: принимает запросы в формате OpenAI Chat Completions API
и перенаправляет их к ApiFreeLLM.

Возможности:
- Автоматическая очередь с учётом rate limit (25 сек)
- Поддержка stream: True (SSE) — нужен для ChatboxAI и CCR
- Умная генерация названий чатов (мгновенно, без API)
- Поддержка мультимодального формата content (строка и массив)
- Автоповтор при 429
"""

import asyncio
import itertools
import json
import re
import time
import uuid
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ====== НАСТРОЙКИ (заполняются при старте сервера) ======
# Не загружаем конфиг при импорте — это ломает PyInstaller сборку.
# Конфиг загружается в lifespan когда приложение реально запускается.
API_KEY: str = ""
API_ENDPOINT: str = ""
DEFAULT_MODEL: str = "apifreellm"
UPSTREAM_TIMEOUT: int = 60
SERVER_HOST: str = "0.0.0.0"
SERVER_PORT: int = 8000

# ====== ОЧЕРЕДЬ ЗАПРОСОВ ======
request_lock = asyncio.Lock()
last_request_time: float = 0.0
RATE_LIMIT_INTERVAL = 25
MAX_RETRIES = 3

# Переиспользуемый HTTP-клиент — создаётся один раз при старте.
# Экономит ~200-500мс на каждом запросе (не нужно заново устанавливать
# TCP-соединение и проходить TLS-рукопожатие).
http_client: httpx.AsyncClient | None = None

# Потокобезопасный счётчик запросов.
# itertools.count — атомарный, не сломается если uvicorn запустит
# несколько воркеров или задачи переключатся между await.
_request_id_generator = itertools.count(1)


# ====== ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ ======

@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    Управление жизненным циклом приложения.
    Код до yield выполняется при запуске сервера.
    Код после yield выполняется при остановке.
    """
    global API_KEY, API_ENDPOINT, DEFAULT_MODEL
    global UPSTREAM_TIMEOUT, SERVER_HOST, SERVER_PORT
    global http_client

    from config import load_config
    config = load_config()

    # Заполняем глобальные переменные из конфига
    API_KEY = config["api_key"]
    API_ENDPOINT = config["api_endpoint"]
    DEFAULT_MODEL = config["model"]
    UPSTREAM_TIMEOUT = config["upstream_timeout_seconds"]
    SERVER_HOST = config["server"]["host"]
    SERVER_PORT = config["server"]["port"]

    # Создаём HTTP-клиент один раз на всё время жизни сервера.
    http_client = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        limits=httpx.Limits(
            max_keepalive_connections=5,
            max_connections=10,
        ),
    )

    logger.info("=" * 50)
    logger.info("Сервер запущен!")
    logger.info("Прокси: http://%s:%d", SERVER_HOST, SERVER_PORT)
    logger.info("Upstream: %s", API_ENDPOINT)
    logger.info("Rate limit: 1 запрос / %d сек", RATE_LIMIT_INTERVAL)
    logger.info("Фильтрация мусорных запросов: ВКЛ")
    logger.info("Поддержка стриминга (SSE): ВКЛ")
    logger.info("Совместимость с CCR: ВКЛ")
    logger.info("Документация: http://localhost:%d/docs", SERVER_PORT)
    logger.info("=" * 50)

    yield  # ← Сервер работает, обрабатывает запросы

    # Код ниже выполняется при остановке сервера
    if http_client:
        await http_client.aclose()
        logger.info("HTTP-клиент закрыт.")
    logger.info("Сервер остановлен.")


app = FastAPI(
    title="ApiFreeLLM Proxy",
    description="OpenAI-совместимый прокси для ApiFreeLLM",
    version="1.0.0",
    lifespan=lifespan,
)


# ====== ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ CONTENT ======

def extract_text_content(content) -> str:
    """
    Извлекает текст из поля content сообщения.

    OpenAI API поддерживает два формата:
    1. Строка: "Привет"
    2. Массив: [{"type": "text", "text": "Привет"}, {"type": "image_url", ...}]

    Некоторые клиенты (CCR) всегда отправляют массив.
    """
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)

    return str(content)


# ====== ФИЛЬТРАЦИЯ И ГЕНЕРАЦИЯ НАЗВАНИЙ ЧАТОВ ======

def is_title_generation_request(messages: list[dict]) -> bool:
    """
    Определяет, является ли запрос попыткой клиента сгенерировать
    название для чата.

    [ИСПРАВЛЕНО] Добавлена проверка длины сообщения.
    Настоящие запросы на генерацию названия — это короткие инструкции
    (обычно до 500 символов). Если сообщение длиннее — это обычный
    запрос пользователя, внутри которого случайно встретился маркер
    (например, пользователь отправил исходный код на ревью, и в коде
    есть строки-маркеры вроде "give this conversation a name").
    """
    if not messages:
        return False

    last_msg = messages[-1]
    last_content = extract_text_content(last_msg.get("content", ""))

    # --- Защита от ложных срабатываний ---
    # Запрос на генерацию названия — это короткая инструкция от клиента.
    # Если сообщение длиннее 1000 символов — это точно НЕ запрос на название,
    # а обычное сообщение пользователя (возможно, с кодом внутри).
    # 1000 символов — с большим запасом, реальные запросы обычно до 500.
    if len(last_content) > 1000:
        return False

    last_content_lower = last_content.lower()

    title_markers = [
        # ChatboxAI
        "give this conversation a name",
        "give a short name",
        "name this conversation",
        "conversation a name",
        "provide the name, nothing else",
        # CCR
        "create a concise title",
        "short title",
        "title for this chat",
        # Общие
        "summarize this conversation",
        "generate a title",
    ]

    for marker in title_markers:
        if marker in last_content_lower:
            return True

    return False


def generate_smart_title(messages: list[dict]) -> str:
    """
    Генерирует осмысленное название чата локально, без API.
    Мгновенно, не тратит rate limit.
    """
    last_content = extract_text_content(messages[-1].get("content", ""))

    # Стратегия 1: достаём текст из блока ``` ... ``` (ChatboxAI)
    user_text = _extract_from_code_block(last_content)
    if user_text:
        return _trim_title(user_text)

    # Стратегия 2: ищем первое сообщение пользователя в массиве (CCR)
    for msg in messages:
        role = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if role == "user" and content and not _is_title_instruction(content):
            return _trim_title(content)

    # Стратегия 3: берём ответ ассистента как fallback
    for msg in messages:
        role = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if role == "assistant" and content:
            return _trim_title(content)

    return "Новый чат"


def _extract_from_code_block(text: str) -> str:
    """
    Достаёт первое сообщение пользователя из блока кода.
    ChatboxAI присылает историю внутри ``` ... ```.
    """
    code_blocks = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)

    if not code_blocks:
        return ""

    block = code_blocks[0]
    lines = block.strip().split("\n")

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("-") and len(stripped) > 1:
            return stripped

    return ""


def _is_title_instruction(text: str) -> bool:
    """Проверяет, является ли текст инструкцией для генерации названия."""
    lower = text.lower()
    instruction_markers = [
        "give this conversation a name",
        "name this conversation",
        "create a concise title",
        "generate a title",
        "provide the name",
        "short title",
        "keep it short",
    ]
    return any(marker in lower for marker in instruction_markers)


def _trim_title(text: str) -> str:
    """
    Обрезает текст до красивого названия чата.
    - Максимум 6 слов
    - Убирает Markdown форматирование
    - Убирает кавычки и префиксы
    """
    first_line = text.strip().split("\n")[0].strip()

    # Убираем Markdown
    first_line = re.sub(r"[*_`#]", "", first_line)

    # Убираем обычные кавычки
    first_line = first_line.strip("\"'`")

    # Убираем юникодные кавычки
    unicode_quotes = [
        "\u00ab", "\u00bb",  # « »
        "\u201e", "\u201c", "\u201d",  # „ " "
        "\u2018", "\u2019",  # ' '
    ]
    for char in unicode_quotes:
        first_line = first_line.strip(char)

    # Убираем префиксы ролей
    for prefix in ["User:", "user:", "Human:", "human:"]:
        if first_line.startswith(prefix):
            first_line = first_line[len(prefix):].strip()

    # Обрезаем до 6 слов
    words = first_line.split()
    title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")

    if len(title) < 2:
        return "Новый чат"

    if len(title) > 50:
        title = title[:47] + "..."

    return title


# ====== СТРИМИНГ (SSE) ======

async def stream_response(text: str, model: str):
    """
    Генератор SSE-ответа — отдаёт текст по частям.

    Все чанки одного ответа имеют одинаковый id и created —
    это требование спецификации OpenAI. Без этого некоторые
    клиенты (ChatboxAI, CCR) могут некорректно группировать чанки.
    """
    # Один ID и timestamp на весь ответ
    completion_id = generate_completion_id()
    created = int(time.time())

    # Первый чанк — сообщает клиенту роль ассистента
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }],
    }
    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

    # Основные чанки с текстом
    chunk_size = 5
    for i in range(0, len(text), chunk_size):
        piece = text[i:i + chunk_size]
        content_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": piece},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.02)

    # Финальный чанк — сигнал клиенту что ответ завершён
    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }
    yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ====== UPSTREAM ======

async def wait_for_rate_limit() -> None:
    """Ждёт нужное время перед следующим запросом к ApiFreeLLM."""
    global last_request_time

    now = time.monotonic()
    elapsed = now - last_request_time
    wait_time = RATE_LIMIT_INTERVAL - elapsed

    if wait_time > 0:
        logger.info("Ожидание rate limit: %.1f сек...", wait_time)
        await asyncio.sleep(wait_time)


async def send_to_upstream(prompt: str, model: str) -> dict:
    """Отправляет запрос к ApiFreeLLM с очередью и повторами."""
    global last_request_time

    async with request_lock:
        for attempt in range(1, MAX_RETRIES + 1):
            await wait_for_rate_limit()

            logger.info(
                "Отправляю запрос к ApiFreeLLM (попытка %d/%d)...",
                attempt,
                MAX_RETRIES,
            )

            # Логируем начало и конец промпта.
            prompt_len = len(prompt)
            if prompt_len > 400:
                prompt_preview = (
                    f"{prompt[:200]}\n"
                    f"... [{prompt_len} символов всего] ...\n"
                    f"{prompt[-200:]}"
                )
            else:
                prompt_preview = prompt
            logger.info("Промпт (%d символов):\n%s", prompt_len, prompt_preview)

            try:
                # Используем глобальный http_client
                response = await http_client.post(
                    API_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "message": prompt,
                        "model": model,
                    },
                )
            except httpx.TimeoutException:
                logger.error("Таймаут (%ds)", UPSTREAM_TIMEOUT)
                raise HTTPException(
                    status_code=504,
                    detail=f"ApiFreeLLM не ответил за {UPSTREAM_TIMEOUT} секунд.",
                )
            except httpx.HTTPError as exc:
                logger.error("Сетевая ошибка: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail="Не удалось подключиться к ApiFreeLLM.",
                )

            last_request_time = time.monotonic()

            if response.status_code == 429:
                try:
                    error_data = response.json()
                    retry_after = error_data.get("retryAfter", RATE_LIMIT_INTERVAL)
                except Exception:
                    retry_after = RATE_LIMIT_INTERVAL

                if attempt < MAX_RETRIES:
                    logger.warning("Rate limit (429). Жду %d сек...", retry_after)
                    await asyncio.sleep(retry_after)
                    last_request_time = time.monotonic()
                    continue
                else:
                    logger.error("Rate limit: все попытки исчерпаны.")
                    raise HTTPException(
                        status_code=429,
                        detail="ApiFreeLLM перегружен. Попробуйте через 30 секунд.",
                    )

            if response.status_code != 200:
                logger.error(
                    "Upstream ошибка: %d — %s",
                    response.status_code,
                    response.text,
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Ошибка от ApiFreeLLM ({response.status_code}): "
                           f"{response.text[:300]}",
                )

            try:
                result_json = response.json()
            except Exception:
                logger.error("Невалидный JSON: %s", response.text)
                raise HTTPException(
                    status_code=502,
                    detail="ApiFreeLLM вернул невалидный JSON.",
                )

            if not result_json.get("success", False):
                logger.warning("success=false: %s", result_json)
                raise HTTPException(
                    status_code=502,
                    detail="ApiFreeLLM сообщил об ошибке.",
                )

            output_text = result_json.get("response", "")

            # Логируем тело ответа
            if len(output_text) > 1000:
                logger.info(
                    "Ответ (%d символов):\n%s\n... [обрезано для лога] ...\n%s",
                    len(output_text),
                    output_text[:500],
                    output_text[-200:],
                )
            else:
                logger.info(
                    "Ответ (%d символов):\n%s",
                    len(output_text),
                    output_text,
                )

            return {"text": output_text}

    raise HTTPException(status_code=500, detail="Неожиданная ошибка.")


# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======

def build_prompt_from_messages(messages: list[dict]) -> str:
    """
    Склеивает массив сообщений OpenAI-формата в один промпт.

    ApiFreeLLM принимает одну строку в поле "message",
    поэтому нам нужно объединить всю историю диалога.
    """
    if not messages:
        return ""

    system_parts = []
    dialogue_parts = []

    role_labels = {
        "user": "User",
        "assistant": "Assistant",
    }

    for msg in messages:
        role = msg.get("role", "user")
        content = extract_text_content(msg.get("content", ""))

        if not content.strip():
            # Пропускаем пустые сообщения — они только засоряют промпт
            continue

        if role == "system":
            # Системные сообщения собираем отдельно —
            # они должны быть в начале как инструкция для модели
            system_parts.append(content)
        else:
            label = role_labels.get(role, role.capitalize())
            dialogue_parts.append(f"{label}: {content}")

    # Если одно сообщение от user и нет system —
    # отправляем без обёрток. Модель лучше понимает
    # чистый текст чем "User: текст".
    if not system_parts and len(dialogue_parts) == 1:
        single_msg = dialogue_parts[0]
        if single_msg.startswith("User: "):
            return single_msg[6:]  # len("User: ") == 6
        return single_msg

    # Собираем финальный промпт
    parts = []

    if system_parts:
        # Явно помечаем как системную инструкцию
        parts.append("[System instruction]\n" + "\n".join(system_parts))

    if dialogue_parts:
        parts.append("\n\n".join(dialogue_parts))

    return "\n\n".join(parts)


def generate_completion_id() -> str:
    """Генерирует уникальный ID ответа в формате OpenAI."""
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


# ====== ЭНДПОИНТЫ ======

@app.post("/v1/chat/completions")
async def proxy_chat(request: Request):
    """Принимает OpenAI-запрос, проксирует в ApiFreeLLM."""
    req_id = next(_request_id_generator)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON.")

    messages = data.get("messages", [])
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="Поле 'messages' обязательно.")

    model = data.get("model", DEFAULT_MODEL)
    use_stream = data.get("stream", False)

    # Детальный лог входящего запроса
    logger.info(
        "[#%d] Получен запрос: model=%s, stream=%s, сообщений=%d",
        req_id, model, use_stream, len(messages),
    )
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = extract_text_content(msg.get("content", ""))
        preview = content[:150] + "..." if len(content) > 150 else content
        logger.info(
            "[#%d]   msg[%d] role=%s len=%d: %s",
            req_id, i, role, len(content), preview,
        )

    # --- Генерация названия чата (мгновенно, без API) ---
    if is_title_generation_request(messages):
        title = generate_smart_title(messages)
        logger.info("[#%d] Название чата -> '%s'", req_id, title)

        if use_stream:
            return StreamingResponse(
                stream_response(title, model),
                media_type="text/event-stream",
            )
        else:
            return JSONResponse(content={
                "id": generate_completion_id(),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": title},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    # --- Настоящий запрос пользователя ---
    prompt = build_prompt_from_messages(messages)

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Все сообщения пустые.")

    logger.info("[#%d] Запрос пользователя: %d сообщений", req_id, len(messages))

    result = await send_to_upstream(prompt, model)

    logger.info("[#%d] Ответ отправлен клиенту.", req_id)

    if use_stream:
        return StreamingResponse(
            stream_response(result["text"], model),
            media_type="text/event-stream",
        )
    else:
        return JSONResponse(content={
            "id": generate_completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result["text"]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


# Запасной маршрут — некоторые клиенты шлют прямо на /v1
@app.post("/v1")
async def proxy_chat_alt(request: Request):
    return await proxy_chat(request)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": 0,
            "owned_by": "apifreellm",
        }],
    }


@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    from config import load_config
    config = load_config()
    uvicorn.run(
        "main:app",
        host=config["server"]["host"],
        port=config["server"]["port"],
        log_level="info",
    )
