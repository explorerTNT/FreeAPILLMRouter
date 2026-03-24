"""
Прокси-сервер: принимает запросы в формате OpenAI Chat Completions API
и перенаправляет их к ApiFreeLLM.

Возможности:
- Автоматическая очередь с учётом rate limit (25 сек)
- Поддержка stream: True (SSE) — нужен для ChatboxAI
- Фильтрация мусорных запросов (генерация названия чата)
- Автоповтор при 429
"""

import asyncio
import json
import time
import uuid
import logging

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from config import load_config

# Загружаем конфиг до создания приложения
config = load_config()

API_KEY = config["api_key"]
API_ENDPOINT = config["api_endpoint"]
DEFAULT_MODEL = config["model"]
UPSTREAM_TIMEOUT = config["upstream_timeout_seconds"]
SERVER_HOST = config["server"]["host"]
SERVER_PORT = config["server"]["port"]

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ApiFreeLLM Proxy",
    description="OpenAI-совместимый прокси для ApiFreeLLM",
    version="1.0.0",
)

# ====== ОЧЕРЕДЬ ЗАПРОСОВ ======
request_lock = asyncio.Lock()
last_request_time: float = 0.0
RATE_LIMIT_INTERVAL = 25
MAX_RETRIES = 3
# =================================

request_counter: int = 0


# ====== ФИЛЬТРАЦИЯ МУСОРНЫХ ЗАПРОСОВ ======

def is_title_generation_request(messages: list[dict]) -> bool:
    """
    Определяет, является ли запрос попыткой ChatboxAI сгенерировать
    название для чата. Такие запросы содержат характерную фразу.
    
    Эти запросы мусорные — они тратят rate limit,
    а пользователь их даже не видит.
    """
    if not messages:
        return False

    # Проверяем последнее сообщение — именно в нём ChatboxAI пишет инструкцию
    last_content = messages[-1].get("content", "").lower()

    # Характерные фразы из запроса ChatboxAI на генерацию названия
    title_markers = [
        "give this conversation a name",
        "give a short name",
        "name this conversation",
        "conversation a name",
        "provide the name, nothing else",
    ]

    for marker in title_markers:
        if marker in last_content:
            return True

    return False


def generate_fake_title(messages: list[dict]) -> str:
    """
    Генерирует название чата локально, без обращения к ApiFreeLLM.
    Берём первое сообщение пользователя и обрезаем до нескольких слов.
    """
    # Ищем текст пользователя внутри запроса на генерацию названия
    last_content = messages[-1].get("content", "")

    # ChatboxAI присылает историю внутри блока ``` ... ```
    # Пытаемся достать оттуда первое сообщение пользователя
    lines = last_content.split("\n")
    user_text = ""

    for line in lines:
        stripped = line.strip()
        # Пропускаем служебные строки
        if stripped and not stripped.startswith("```") and not stripped.startswith("-"):
            # Пропускаем саму инструкцию ChatboxAI (на английском)
            if "conversation" not in stripped.lower() and "name" not in stripped.lower():
                user_text = stripped
                break

    if not user_text:
        return "Новый чат"

    # Обрезаем до 5 слов
    words = user_text.split()[:5]
    return " ".join(words)


# ====== СТРИМИНГ (SSE) ======

def create_stream_chunk(content: str, model: str, finish_reason: str = None) -> str:
    """
    Формирует один чанк SSE-ответа в формате OpenAI.
    
    ChatboxAI ожидает именно такой формат — без него
    клиент не может прочитать ответ модели.
    """
    chunk = {
        "id": generate_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
    }

    # Первый чанк содержит роль, последний — finish_reason, промежуточные — контент
    if finish_reason is None:
        chunk["choices"][0]["delta"] = {"content": content}
    
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def stream_response(text: str, model: str):
    """
    Генератор SSE-ответа — отдаёт текст по частям,
    имитируя стриминг как у OpenAI.
    
    Разбиваем на небольшие куски чтобы ChatboxAI
    показывал текст постепенно.
    """
    # Первый чанк — роль ассистента
    first_chunk = {
        "id": generate_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

    # Разбиваем текст на куски по ~5 символов (имитация стриминга)
    chunk_size = 5
    for i in range(0, len(text), chunk_size):
        piece = text[i:i + chunk_size]
        yield create_stream_chunk(piece, model)
        # Маленькая пауза — чтобы ChatboxAI успевал отрисовывать
        await asyncio.sleep(0.02)

    # Финальный чанк — сигнал что генерация завершена
    yield create_stream_chunk("", model, finish_reason="stop")

    # Маркер конца потока — стандарт OpenAI SSE
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

            try:
                async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
                    response = await client.post(
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
                    logger.warning(
                        "Rate limit (429). Жду %d сек...", retry_after
                    )
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
                    detail={
                        "error": "Ошибка от ApiFreeLLM",
                        "upstream_body": response.text,
                    },
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
            logger.info("Ответ получен: %d символов", len(output_text))
            return {"text": output_text}

    raise HTTPException(status_code=500, detail="Неожиданная ошибка.")


# ====== ЭНДПОИНТЫ ======

@app.on_event("startup")
async def on_startup():
    logger.info("=" * 50)
    logger.info("Сервер запущен!")
    logger.info("Прокси: http://%s:%d", SERVER_HOST, SERVER_PORT)
    logger.info("Upstream: %s", API_ENDPOINT)
    logger.info("Rate limit: 1 запрос / %d сек", RATE_LIMIT_INTERVAL)
    logger.info("Фильтрация мусорных запросов: ВКЛ")
    logger.info("Поддержка стриминга (SSE): ВКЛ")
    logger.info("Документация: http://localhost:%d/docs", SERVER_PORT)
    logger.info("=" * 50)


def build_prompt_from_messages(messages: list[dict]) -> str:
    """Склеивает массив сообщений OpenAI-формата в один промпт."""
    if not messages:
        return ""

    role_labels = {
        "system": "System",
        "user": "User",
        "assistant": "Assistant",
    }

    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        label = role_labels.get(role, role.capitalize())
        parts.append(f"{label}: {content}")

    return "\n\n".join(parts)


def generate_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


@app.post("/v1/chat/completions")
async def proxy_chat(request: Request):
    """Принимает OpenAI-запрос, проксирует в ApiFreeLLM."""
    global request_counter
    request_counter += 1
    req_id = request_counter

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON.")

    messages = data.get("messages", [])
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="Поле 'messages' обязательно.")

    model = data.get("model", DEFAULT_MODEL)
    use_stream = data.get("stream", False)

    # --- Фильтруем мусорные запросы ---
    if is_title_generation_request(messages):
        title = generate_fake_title(messages)
        logger.info(
            "[#%d] Запрос на название чата — отвечаю локально: '%s'",
            req_id,
            title,
        )

        # Отдаём название без обращения к ApiFreeLLM
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

    # --- Стриминговый или обычный ответ ---
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
    uvicorn.run(
        "main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
    )
