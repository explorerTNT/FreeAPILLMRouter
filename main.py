"""
Прокси-сервер: принимает запросы в формате OpenAI Chat Completions API
и перенаправляет их к ApiFreeLLM.

Возможности:
- Автоматическая очередь с учётом rate limit (25 сек)
- Поддержка stream: True (SSE) — нужен для ChatboxAI и CCR
- Умная генерация названий чатов (мгновенно, без API)
- Поддержка мультимодального формата content (строка и массив)
- Автоповтор при 429
- Подсчёт использованных токенов (приблизительный)
- Умное сжатие контекста при превышении 32k токенов
- Веб-панель мониторинга с реальной статистикой
- Эмуляция tool calling для CCR / Claude Code
"""

import asyncio
import hashlib
import itertools
import json
import re
import statistics
import time
import uuid
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import httpx
import tool_emulation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ====== НАСТРОЙКИ ======
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

http_client: httpx.AsyncClient | None = None
_request_id_generator = itertools.count(1)

# ====== ЛИМИТ КОНТЕКСТА ======
CONTEXT_LIMIT_TOKENS = 32_000
RESPONSE_RESERVE_TOKENS = 4_000
MAX_PROMPT_TOKENS = CONTEXT_LIMIT_TOKENS - RESPONSE_RESERVE_TOKENS

# ====== ДЕТЕКТОР ЦИКЛА ======
LOOP_DETECTION_THRESHOLD = 3
LOOP_SESSION_TTL = 600


# ====== ПОДСЧЁТ ТОКЕНОВ ======

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cyrillic_count = sum(1 for ch in text if '\u0400' <= ch <= '\u04ff')
    total_chars = len(text)
    if total_chars == 0:
        return 0
    cyrillic_ratio = cyrillic_count / total_chars
    chars_per_token = 2.0 * cyrillic_ratio + 4.0 * (1.0 - cyrillic_ratio)
    estimated = int(total_chars / chars_per_token * 1.1)
    return max(estimated, 1)


# ====== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ДЕТЕКТОРА ЦИКЛА ======

def _last_tool_result_is_error(messages: list[dict]) -> bool:
    """
    Проверяет содержит ли последний tool result сообщение об ошибке.

    Используется детектором цикла чтобы НЕ блокировать Read/Write
    когда модель восстанавливается после упавшего Edit.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = extract_text_content(msg.get("content", ""))
        if not content.startswith("[Tool Result"):
            continue
        content_lower = content.lower()
        error_markers = [
            "error", "not found", "failed", "cannot",
            "no such file", "string not found",
            "file must be read first", "exception",
        ]
        return any(marker in content_lower for marker in error_markers)
    return False


# ====== ДЕТЕКТОР ЗАЦИКЛИВАНИЯ ======

@dataclass
class _ToolCallRecord:
    tool_name: str
    args_hash: str


@dataclass
class _AgentSession:
    call_counts: dict[str, int] = field(default_factory=dict)
    call_arg_hashes: dict[str, set] = field(default_factory=dict)
    last_active: float = field(default_factory=time.monotonic)
    pressure_count: int = 0


class LoopDetector:
    """
    Отслеживает паттерны зацикливания агента.

    Read/Write после ошибки инструмента — это НЕ цикл,
    это нормальное восстановление. Детектор пропускает такие случаи.
    """

    def __init__(self):
        self._sessions: dict[str, _AgentSession] = {}

    def _get_session(self, conv_id: str) -> _AgentSession:
        now = time.monotonic()
        if conv_id in self._sessions:
            session = self._sessions[conv_id]
            if now - session.last_active > LOOP_SESSION_TTL:
                logger.info(
                    "Loop detector: session %s expired, resetting", conv_id
                )
                self._sessions[conv_id] = _AgentSession()
        else:
            self._sessions[conv_id] = _AgentSession()
        self._sessions[conv_id].last_active = now
        self._cleanup_old_sessions()
        return self._sessions[conv_id]

    def _cleanup_old_sessions(self) -> None:
        if len(self._sessions) < 50:
            return
        now = time.monotonic()
        stale = [
            k for k, v in self._sessions.items()
            if now - v.last_active > LOOP_SESSION_TTL
        ]
        for key in stale:
            del self._sessions[key]

    @staticmethod
    def _hash_args(arguments: dict | list) -> str:
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(canonical.encode()).hexdigest()[:8]

    def record_tool_calls(self, conv_id: str, tool_calls: list[dict]) -> None:
        session = self._get_session(conv_id)
        for tc in tool_calls:
            name = tc.get("tool_name", "unknown")
            args = tc.get("arguments", {})
            args_hash = self._hash_args(args)
            session.call_counts[name] = session.call_counts.get(name, 0) + 1
            if name not in session.call_arg_hashes:
                session.call_arg_hashes[name] = set()
            session.call_arg_hashes[name].add(args_hash)

    def check_and_build_warning(
        self, conv_id: str, messages: list[dict],
    ) -> str:
        session = self._get_session(conv_id)
        warnings = []

        last_result_is_error = _last_tool_result_is_error(messages)
        recovery_tools = {"Read", "Write"}

        for tool_name, count in session.call_counts.items():
            if count >= LOOP_DETECTION_THRESHOLD:
                if tool_name in recovery_tools and last_result_is_error:
                    logger.debug(
                        "Loop detector: skipping %s (recovery after error)",
                        tool_name,
                    )
                    continue
                warnings.append(
                    f"- You have already called '{tool_name}' "
                    f"{count} times in this session."
                )

        for tool_name, hashes in session.call_arg_hashes.items():
            count = session.call_counts.get(tool_name, 0)
            if count > len(hashes) and count >= 2:
                if tool_name in recovery_tools and last_result_is_error:
                    logger.debug(
                        "Loop detector: skipping %s same-args "
                        "(recovery after error)", tool_name,
                    )
                    continue
                warnings.append(
                    f"- You have called '{tool_name}' with the same "
                    f"arguments more than once."
                )

        if not warnings:
            return ""

        session.pressure_count += 1

        if session.pressure_count >= 3:
            directive = (
                "STOP using tools. You have enough information. "
                "Write the final code changes NOW using Write or Edit tools, "
                "or provide your final answer as plain text."
            )
        else:
            directive = (
                "You likely already have the information you need. "
                "Stop searching and start making the actual changes. "
                "Use Write or Edit tools to apply fixes."
            )

        warning_text = (
            "[LOOP DETECTED]\n"
            "You appear to be repeating the same actions:\n"
            + "\n".join(warnings) + "\n\n"
            + directive
        )

        logger.warning(
            "Loop detected for session %s: %s (pressure #%d)",
            conv_id, warnings, session.pressure_count,
        )

        return warning_text

    def reset_session(self, conv_id: str) -> None:
        if conv_id in self._sessions:
            del self._sessions[conv_id]


loop_detector = LoopDetector()


def get_conversation_id(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            content = extract_text_content(msg.get("content", ""))
            seed = content[:200]
            return hashlib.md5(seed.encode()).hexdigest()[:12]
    all_content = "".join(
        extract_text_content(m.get("content", "")) for m in messages
    )
    return hashlib.md5(all_content[:500].encode()).hexdigest()[:12]


# ====== СТАТИСТИКА ======

@dataclass
class ProxyStats:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    title_requests_filtered: int = 0
    tool_call_requests: int = 0
    tool_calls_emulated: int = 0
    loop_detections: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    response_times: list[float] = field(default_factory=list)
    context_truncations: int = 0
    start_time: float = field(default_factory=time.time)

    def record_success(self, response_time, prompt_tokens, completion_tokens):
        self.total_requests += 1
        self.successful_requests += 1
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.response_times.append(response_time)
        if len(self.response_times) > 100:
            self.response_times.pop(0)

    def record_failure(self):
        self.total_requests += 1
        self.failed_requests += 1

    def record_title_filtered(self):
        self.total_requests += 1
        self.title_requests_filtered += 1

    def record_truncation(self):
        self.context_truncations += 1

    def record_tool_request(self):
        self.tool_call_requests += 1

    def record_tool_emulated(self):
        self.tool_calls_emulated += 1

    def record_loop_detection(self):
        self.loop_detections += 1

    @property
    def total_tokens(self):
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def avg_response_time(self):
        if not self.response_times:
            return 0.0
        return statistics.mean(self.response_times)

    @property
    def uptime_seconds(self):
        return time.time() - self.start_time

    def to_dict(self):
        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "uptime_human": _format_duration(self.uptime_seconds),
            "total_requests": self.total_requests,
            "successful": self.successful_requests,
            "failed": self.failed_requests,
            "title_filtered": self.title_requests_filtered,
            "tool_calls": {
                "requests_with_tools": self.tool_call_requests,
                "successfully_emulated": self.tool_calls_emulated,
                "loop_detections": self.loop_detections,
            },
            "tokens": {
                "prompt": self.total_prompt_tokens,
                "completion": self.total_completion_tokens,
                "total": self.total_tokens,
                "total_human": _format_tokens(self.total_tokens),
            },
            "context_truncations": self.context_truncations,
            "avg_response_time_seconds": round(self.avg_response_time, 2),
            "rate_limit_interval": RATE_LIMIT_INTERVAL,
            "context_limit_tokens": CONTEXT_LIMIT_TOKENS,
        }


def _format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_tokens(tokens: int) -> str:
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}k"


proxy_stats = ProxyStats()


# ====== ЖИЗНЕННЫЙ ЦИКЛ ======

@asynccontextmanager
async def lifespan(application: FastAPI):
    global API_KEY, API_ENDPOINT, DEFAULT_MODEL
    global UPSTREAM_TIMEOUT, SERVER_HOST, SERVER_PORT
    global http_client

    from config import load_config
    config = load_config()

    API_KEY = config["api_key"]
    API_ENDPOINT = config["api_endpoint"]
    DEFAULT_MODEL = config["model"]
    UPSTREAM_TIMEOUT = config["upstream_timeout_seconds"]
    SERVER_HOST = config["server"]["host"]
    SERVER_PORT = config["server"]["port"]

    http_client = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        limits=httpx.Limits(
            max_keepalive_connections=5,
            max_connections=10,
        ),
    )

    logger.info("=" * 50)
    logger.info("Server started!")
    logger.info("Proxy: http://%s:%d", SERVER_HOST, SERVER_PORT)
    logger.info("Upstream: %s", API_ENDPOINT)
    logger.info("Rate limit: %ds | Context: %dk (reserve %dk)",
                RATE_LIMIT_INTERVAL,
                CONTEXT_LIMIT_TOKENS // 1000,
                RESPONSE_RESERVE_TOKENS // 1000)
    logger.info("Tool emulation: ENABLED")
    logger.info("Loop detection: threshold=%d, session_ttl=%ds",
                LOOP_DETECTION_THRESHOLD, LOOP_SESSION_TTL)
    logger.info("Dashboard: http://localhost:%d/", SERVER_PORT)
    logger.info("API Docs: http://localhost:%d/docs", SERVER_PORT)
    logger.info("=" * 50)

    yield

    if http_client:
        await http_client.aclose()

    logger.info(
        "Session ended: %d requests, ~%s tokens, %d tool calls, "
        "%d loops detected",
        proxy_stats.total_requests,
        _format_tokens(proxy_stats.total_tokens),
        proxy_stats.tool_calls_emulated,
        proxy_stats.loop_detections,
    )


app = FastAPI(
    title="ApiFreeLLM Proxy",
    description="OpenAI-compatible proxy for ApiFreeLLM with tool calling emulation",
    version="2.3.0",
    lifespan=lifespan,
)


# ====== ИЗВЛЕЧЕНИЕ ТЕКСТА ======

def extract_text_content(content) -> str:
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
    if not messages:
        return False
    last_msg = messages[-1]
    last_content = extract_text_content(last_msg.get("content", ""))
    if len(last_content) > 1000:
        return False
    last_content_lower = last_content.lower()
    title_markers = [
        "give this conversation a name",
        "give a short name",
        "name this conversation",
        "conversation a name",
        "provide the name, nothing else",
        "create a concise title",
        "short title",
        "title for this chat",
        "summarize this conversation",
        "generate a title",
    ]
    return any(marker in last_content_lower for marker in title_markers)


def generate_smart_title(messages: list[dict]) -> str:
    last_content = extract_text_content(messages[-1].get("content", ""))
    user_text = _extract_from_code_block(last_content)
    if user_text:
        return _trim_title(user_text)
    for msg in messages:
        role = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if role == "user" and content and not _is_title_instruction(content):
            return _trim_title(content)
    for msg in messages:
        role = msg.get("role", "")
        content = extract_text_content(msg.get("content", ""))
        if role == "assistant" and content:
            return _trim_title(content)
    return "New chat"


def _extract_from_code_block(text: str) -> str:
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
    first_line = text.strip().split("\n")[0].strip()
    first_line = re.sub(r"[*_`#]", "", first_line)
    first_line = first_line.strip("\"'`")
    for char in ["\u00ab", "\u00bb", "\u201e", "\u201c", "\u201d",
                 "\u2018", "\u2019"]:
        first_line = first_line.strip(char)
    for prefix in ["User:", "user:", "Human:", "human:"]:
        if first_line.startswith(prefix):
            first_line = first_line[len(prefix):].strip()
    words = first_line.split()
    title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")
    if len(title) < 2:
        return "New chat"
    if len(title) > 50:
        title = title[:47] + "..."
    return title


# ====== УМНОЕ СЖАТИЕ КОНТЕКСТА ======

def trim_messages_to_fit(
    messages: list[dict],
    max_tokens: int,
    reserved_for_tools: int = 0,
) -> list[dict]:
    if not messages:
        return messages

    effective_max = max_tokens - reserved_for_tools

    if effective_max < 500:
        logger.warning(
            "Tools reserve (%d) leaves only %d tokens for messages, "
            "forcing minimum 500",
            reserved_for_tools, effective_max,
        )
        effective_max = 500

    msg_tokens = []
    for msg in messages:
        content = extract_text_content(msg.get("content", ""))
        msg_tokens.append(estimate_tokens(content))

    total_tokens = sum(msg_tokens)

    if total_tokens <= effective_max:
        return messages

    proxy_stats.record_truncation()
    logger.info(
        "Context: ~%d tokens (+ ~%d tools = ~%d) > %d limit, trimming...",
        total_tokens, reserved_for_tools,
        total_tokens + reserved_for_tools, max_tokens,
    )

    system_msgs = []
    system_tokens = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            system_msgs.append(msg)
            system_tokens += msg_tokens[i]

    last_user_msg = None
    last_user_tokens = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_msg = messages[i]
            last_user_tokens = msg_tokens[i]
            break

    reserved_tokens = system_tokens + last_user_tokens + 50

    if reserved_tokens >= effective_max:
        logger.warning(
            "System + question exceed limit (%d > %d), sending question only",
            reserved_tokens, effective_max,
        )
        if last_user_msg:
            return [last_user_msg]
        return [messages[-1]]

    remaining_budget = effective_max - reserved_tokens

    dialogue_msgs = []
    dialogue_tokens_list = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            continue
        dialogue_msgs.append(msg)
        dialogue_tokens_list.append(msg_tokens[i])

    kept_msgs = []
    kept_tokens = 0

    for i in range(len(dialogue_msgs) - 1, -1, -1):
        if dialogue_msgs[i] is last_user_msg:
            continue
        if kept_tokens + dialogue_tokens_list[i] > remaining_budget:
            break
        kept_msgs.insert(0, dialogue_msgs[i])
        kept_tokens += dialogue_tokens_list[i]

    trimmed_count = len(dialogue_msgs) - len(kept_msgs) - 1
    result = list(system_msgs)

    if trimmed_count > 0:
        result.append({
            "role": "system",
            "content": (
                f"[{trimmed_count} previous messages were omitted "
                f"due to context limit.]"
            ),
        })

    result.extend(kept_msgs)

    if last_user_msg and last_user_msg not in result:
        result.append(last_user_msg)

    final_tokens = sum(
        estimate_tokens(extract_text_content(m.get("content", "")))
        for m in result
    )
    logger.info(
        "Trimmed: %d->%d messages (removed %d), "
        "~%d msg tokens + ~%d tools = ~%d total",
        len(messages), len(result), trimmed_count,
        final_tokens, reserved_for_tools,
        final_tokens + reserved_for_tools,
    )

    return result


# ====== СТРИМИНГ ======

async def stream_response(text: str, model: str):
    completion_id = generate_completion_id()
    created = int(time.time())
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": ""},
                     "finish_reason": None}],
    }
    try:
        yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"
        chunk_size = 5
        for i in range(0, len(text), chunk_size):
            piece = text[i:i + chunk_size]
            content_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": piece},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.02)
        stop_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        logger.info("Client disconnected during streaming")
        return


async def stream_tool_calls_response(tool_calls: list[dict], model: str):
    completion_id = generate_completion_id()
    events = tool_emulation.build_tool_calls_stream_events(
        tool_calls, model, completion_id
    )
    try:
        for event in events:
            yield event
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        logger.info("Client disconnected during tool call streaming")
        return


# ====== UPSTREAM ======

async def wait_for_rate_limit() -> None:
    global last_request_time
    now = time.monotonic()
    elapsed = now - last_request_time
    wait_time = RATE_LIMIT_INTERVAL - elapsed
    if wait_time > 0:
        logger.info("Rate limit: waiting %.0fs...", wait_time)
        await asyncio.sleep(wait_time)


async def send_to_upstream(prompt: str, model: str) -> dict:
    global last_request_time

    async with request_lock:
        for attempt in range(1, MAX_RETRIES + 1):
            await wait_for_rate_limit()

            if attempt > 1:
                logger.info("Retry %d/%d...", attempt, MAX_RETRIES)

            try:
                response = await http_client.post(
                    API_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={"message": prompt, "model": model},
                )
            except httpx.TimeoutException:
                logger.error("Timeout (%ds)", UPSTREAM_TIMEOUT)
                raise HTTPException(
                    status_code=504,
                    detail=f"ApiFreeLLM did not respond within "
                           f"{UPSTREAM_TIMEOUT} seconds.",
                )
            except httpx.HTTPError as exc:
                logger.error("Network error: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail="Could not connect to ApiFreeLLM.",
                )

            last_request_time = time.monotonic()

            if response.status_code == 503:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "503 Service unavailable, retrying in %ds...",
                        RATE_LIMIT_INTERVAL,
                    )
                    await asyncio.sleep(RATE_LIMIT_INTERVAL)
                    last_request_time = time.monotonic()
                    continue
                else:
                    logger.error("503: all %d attempts exhausted", MAX_RETRIES)
                    raise HTTPException(
                        status_code=503,
                        detail="ApiFreeLLM is temporarily unavailable.",
                    )

            if response.status_code == 429:
                try:
                    error_data = response.json()
                    retry_after = error_data.get(
                        "retryAfter", RATE_LIMIT_INTERVAL
                    )
                except Exception:
                    retry_after = RATE_LIMIT_INTERVAL

                if attempt < MAX_RETRIES:
                    logger.warning(
                        "429 Rate limit, waiting %ds...", retry_after
                    )
                    await asyncio.sleep(retry_after)
                    last_request_time = time.monotonic()
                    continue
                else:
                    logger.error("429: all %d attempts exhausted", MAX_RETRIES)
                    raise HTTPException(
                        status_code=429,
                        detail="ApiFreeLLM is overloaded. Try again in 30s.",
                    )

            if response.status_code != 200:
                logger.error(
                    "Upstream %d: %s",
                    response.status_code, response.text[:200],
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"ApiFreeLLM error ({response.status_code}): "
                           f"{response.text[:300]}",
                )

            try:
                result_json = response.json()
            except Exception:
                logger.error("Invalid JSON from upstream")
                raise HTTPException(
                    status_code=502,
                    detail="ApiFreeLLM returned invalid JSON.",
                )

            if not result_json.get("success", False):
                logger.warning("success=false from upstream")
                raise HTTPException(
                    status_code=502,
                    detail="ApiFreeLLM reported an error.",
                )

            return {"text": result_json.get("response", "")}

    raise HTTPException(status_code=500, detail="Unexpected error.")


# ====== СБОРКА ПРОМПТА ======

def build_prompt_from_messages(
    messages: list[dict],
    tools_prompt: str = "",
    loop_warning: str = "",
) -> str:
    if not messages:
        return ""

    system_parts = []
    dialogue_parts = []
    role_labels = {"user": "User", "assistant": "Assistant"}

    for msg in messages:
        role = msg.get("role", "user")
        content = extract_text_content(msg.get("content", ""))
        if not content.strip():
            continue
        if role == "system":
            system_parts.append(content)
        else:
            label = role_labels.get(role, role.capitalize())
            dialogue_parts.append(f"{label}: {content}")

    if tools_prompt:
        system_parts.append(tools_prompt)

    if loop_warning:
        system_parts.append(loop_warning)

    if not system_parts and len(dialogue_parts) == 1:
        single_msg = dialogue_parts[0]
        if single_msg.startswith("User: "):
            return single_msg[6:]
        return single_msg

    parts = []
    if system_parts:
        parts.append("[System instruction]\n" + "\n".join(system_parts))
    if dialogue_parts:
        parts.append("\n\n".join(dialogue_parts))

    return "\n\n".join(parts)


def generate_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


# ====== ВЕБ-ДАШБОРД ======

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ApiFreeLLM Proxy</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                         Roboto, 'Helvetica Neue', sans-serif;
            background: #0f0f1a; color: #e0e0e0;
            min-height: 100vh; padding: 24px;
        }
        .container { max-width: 720px; margin: 0 auto; }
        header { text-align: center; margin-bottom: 32px; }
        header h1 { font-size: 28px; color: #fff; margin-bottom: 8px; }
        .status-badge {
            display: inline-flex; align-items: center; gap: 8px;
            background: rgba(76, 175, 80, 0.15);
            border: 1px solid rgba(76, 175, 80, 0.3);
            border-radius: 20px; padding: 6px 16px;
            font-size: 14px; color: #4caf50;
        }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #4caf50; animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(76,175,80,0.4); }
            50% { opacity: 0.7; box-shadow: 0 0 0 8px rgba(76,175,80,0); }
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px; margin-bottom: 24px;
        }
        .card {
            background: #1a1a2e; border: 1px solid #2a2a3e;
            border-radius: 12px; padding: 20px; transition: border-color 0.2s;
        }
        .card:hover { border-color: #3a3a5e; }
        .card-label {
            font-size: 12px; color: #888; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 8px;
        }
        .card-value { font-size: 32px; font-weight: 700; color: #fff; }
        .card-value.accent { color: #e94560; }
        .card-value.green { color: #4caf50; }
        .card-value.blue { color: #64b5f6; }
        .card-value.orange { color: #ffb74d; }
        .card-value.purple { color: #ce93d8; }
        .card-sub { font-size: 12px; color: #666; margin-top: 4px; }
        .wide-card { grid-column: 1 / -1; }
        .token-bar {
            margin-top: 12px; background: #2a2a3e;
            border-radius: 6px; height: 8px; overflow: hidden;
        }
        .token-bar-fill {
            height: 100%; border-radius: 6px; transition: width 0.5s ease;
            background: linear-gradient(90deg, #4caf50, #ffb74d, #e94560);
        }
        .token-details {
            display: flex; justify-content: space-between;
            margin-top: 8px; font-size: 12px; color: #888;
        }
        .setup-card {
            background: #1a1a2e; border: 1px solid #2a2a3e;
            border-radius: 12px; padding: 24px; margin-top: 24px;
        }
        .setup-card h3 { color: #fff; margin-bottom: 16px; font-size: 16px; }
        .setup-row {
            display: flex; justify-content: space-between;
            align-items: center; padding: 8px 0;
            border-bottom: 1px solid #2a2a3e;
        }
        .setup-row:last-child { border-bottom: none; }
        .setup-label { color: #888; font-size: 14px; }
        .setup-value {
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 13px; color: #64b5f6; background: #0f0f1a;
            padding: 4px 12px; border-radius: 6px; cursor: pointer;
            border: 1px solid transparent; transition: border-color 0.2s;
        }
        .setup-value:hover { border-color: #64b5f6; }
        .setup-value.copied { border-color: #4caf50; color: #4caf50; }
        .footer {
            text-align: center; margin-top: 32px;
            font-size: 12px; color: #444;
        }
        .footer a { color: #64b5f6; text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>ApiFreeLLM Proxy</h1>
            <div class="status-badge">
                <div class="status-dot"></div>
                Running | <span id="uptime">-</span>
            </div>
        </header>
        <div class="grid">
            <div class="card">
                <div class="card-label">Requests processed</div>
                <div class="card-value green" id="total-requests">-</div>
                <div class="card-sub">
                    <span id="failed-requests">0</span> errors |
                    <span id="filtered-requests">0</span> filtered
                </div>
            </div>
            <div class="card">
                <div class="card-label">Avg response time</div>
                <div class="card-value blue" id="avg-time">-</div>
                <div class="card-sub">Rate limit: __RATE_LIMIT__s between requests</div>
            </div>
            <div class="card">
                <div class="card-label">Tool calls emulated</div>
                <div class="card-value purple" id="tool-calls">-</div>
                <div class="card-sub">
                    <span id="tool-requests">0</span> requests with tools |
                    <span id="loop-detections">0</span> loops broken
                </div>
            </div>
            <div class="card wide-card">
                <div class="card-label">Tokens used (approximate)</div>
                <div class="card-value accent" id="total-tokens">-</div>
                <div class="token-bar">
                    <div class="token-bar-fill" id="token-bar" style="width: 0%"></div>
                </div>
                <div class="token-details">
                    <span>Prompt: <strong id="prompt-tokens">0</strong></span>
                    <span>Completion: <strong id="completion-tokens">0</strong></span>
                    <span>Context trimmed: <strong id="truncations">0</strong>x</span>
                </div>
            </div>
            <div class="card">
                <div class="card-label">Context limit</div>
                <div class="card-value orange">__CONTEXT_LIMIT__</div>
                <div class="card-sub">Response reserve: __RESPONSE_RESERVE__</div>
            </div>
            <div class="card">
                <div class="card-value" style="font-size: 20px; color: #ce93d8;"
                    id="model-name">__MODEL__</div>
                <div class="card-sub">ApiFreeLLM Free Tier</div>
            </div>
        </div>
        <div class="setup-card">
            <h3>Client settings</h3>
            <div class="setup-row">
                <span class="setup-label">API URL</span>
                <span class="setup-value" onclick="copyText(this)"
                    >http://localhost:__PORT__/v1</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">Full endpoint</span>
                <span class="setup-value" onclick="copyText(this)"
                    >http://localhost:__PORT__/v1/chat/completions</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">API Key</span>
                <span class="setup-value" onclick="copyText(this)">any-text</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">Model</span>
                <span class="setup-value" onclick="copyText(this)">__MODEL__</span>
            </div>
        </div>
        <div class="setup-card">
            <h3>CCR (Claude Code Router) settings</h3>
            <div class="setup-row">
                <span class="setup-label">api_base_url</span>
                <span class="setup-value" onclick="copyText(this)"
                    >http://localhost:__PORT__/v1/chat/completions</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">api_key</span>
                <span class="setup-value" onclick="copyText(this)">any-text</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">model</span>
                <span class="setup-value" onclick="copyText(this)">apifreellm</span>
            </div>
            <div class="setup-row">
                <span class="setup-label">transformer</span>
                <span class="setup-value" style="color: #ffb74d;">not required</span>
            </div>
        </div>
        <div class="footer">
            <a href="/docs">API Docs</a> |
            <a href="/v1/stats">Stats JSON</a> |
            <a href="https://apifreellm.com" target="_blank">ApiFreeLLM</a>
        </div>
    </div>
    <script>
        function copyText(el) {
            const text = el.textContent;
            navigator.clipboard.writeText(text).then(() => {
                el.classList.add('copied');
                const orig = el.textContent;
                el.textContent = 'Copied!';
                setTimeout(() => { el.textContent = orig; el.classList.remove('copied'); }, 1500);
            });
        }
        function fmt(n) {
            return n.toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g, ' ');
        }
        async function updateStats() {
            try {
                const r = await fetch('/v1/stats');
                const d = await r.json();
                document.getElementById('uptime').textContent = d.uptime_human;
                document.getElementById('total-requests').textContent = fmt(d.successful);
                document.getElementById('failed-requests').textContent = d.failed;
                document.getElementById('filtered-requests').textContent = d.title_filtered;
                document.getElementById('avg-time').textContent = d.avg_response_time_seconds + 's';
                document.getElementById('total-tokens').textContent = d.tokens.total_human;
                document.getElementById('prompt-tokens').textContent = fmt(d.tokens.prompt);
                document.getElementById('completion-tokens').textContent = fmt(d.tokens.completion);
                document.getElementById('truncations').textContent = d.context_truncations;
                document.getElementById('tool-calls').textContent = fmt(d.tool_calls.successfully_emulated);
                document.getElementById('tool-requests').textContent = d.tool_calls.requests_with_tools;
                document.getElementById('loop-detections').textContent = d.tool_calls.loop_detections;
                const pct = Math.min((d.tokens.total / 1000000) * 100, 100);
                document.getElementById('token-bar').style.width = pct + '%';
            } catch(e) {}
        }
        updateStats();
        setInterval(updateStats, 3000);
    </script>
</body>
</html>"""


# ====== ЭНДПОИНТЫ ======

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = DASHBOARD_HTML
    html = html.replace("__PORT__", str(SERVER_PORT))
    html = html.replace("__MODEL__", DEFAULT_MODEL)
    html = html.replace("__RATE_LIMIT__", str(RATE_LIMIT_INTERVAL))
    html = html.replace("__CONTEXT_LIMIT__", f"{CONTEXT_LIMIT_TOKENS // 1000}k")
    html = html.replace("__RESPONSE_RESERVE__", f"{RESPONSE_RESERVE_TOKENS // 1000}k")
    return HTMLResponse(content=html)


@app.get("/v1/stats")
async def get_stats():
    return proxy_stats.to_dict()


@app.post("/v1/chat/completions")
async def proxy_chat(request: Request):
    req_id = next(_request_id_generator)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON.")

    messages = data.get("messages", [])
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="Field 'messages' is required.")

    model = data.get("model", DEFAULT_MODEL)
    use_stream = data.get("stream", False)
    is_tool_request = tool_emulation.has_tools(data)
    tools = data.get("tools", [])

    total_content_len = sum(
        len(extract_text_content(m.get("content", ""))) for m in messages
    )
    logger.info(
        "[#%d] Request: %d messages, ~%d chars, stream=%s, tools=%d",
        req_id, len(messages), total_content_len, use_stream,
        len(tools) if is_tool_request else 0,
    )

    if is_tool_request:
        proxy_stats.record_tool_request()

    if is_title_generation_request(messages):
        title = generate_smart_title(messages)
        logger.info("[#%d] Chat title: '%s'", req_id, title)
        proxy_stats.record_title_filtered()
        if use_stream:
            return StreamingResponse(
                stream_response(title, model),
                media_type="text/event-stream",
            )
        return JSONResponse(content={
            "id": generate_completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": title},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    conv_id = get_conversation_id(messages)

    if tool_emulation.has_tool_results(messages):
        messages = tool_emulation.convert_tool_messages_to_text(messages)
        logger.info("[#%d] Converted tool results to text format", req_id)

    tools_prompt = ""
    tools_token_reserve = 0
    if is_tool_request:
        tools_prompt = tool_emulation.build_tools_system_prompt(tools)
        tools_token_reserve = estimate_tokens(tools_prompt)
        logger.info(
            "[#%d] Tool emulation: %d tools -> ~%d tokens reserved",
            req_id, len(tools), tools_token_reserve,
        )

    loop_warning = ""
    if is_tool_request:
        loop_warning = loop_detector.check_and_build_warning(conv_id, messages)
        if loop_warning:
            proxy_stats.record_loop_detection()
            tools_token_reserve += estimate_tokens(loop_warning)

    trimmed_messages = trim_messages_to_fit(
        messages, MAX_PROMPT_TOKENS, reserved_for_tools=tools_token_reserve
    )

    prompt = build_prompt_from_messages(trimmed_messages, tools_prompt, loop_warning)

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="All messages are empty.")

    prompt_tokens = estimate_tokens(prompt)
    logger.info("[#%d] Prompt: ~%d tokens", req_id, prompt_tokens)

    start_time = time.monotonic()
    try:
        result = await send_to_upstream(prompt, model)
    except HTTPException:
        proxy_stats.record_failure()
        raise

    elapsed = time.monotonic() - start_time
    response_text = result["text"]

    # Дебаг-лог — увеличен до 1500 символов чтобы видеть полный JSON с закрывающим ```
    if is_tool_request:
        logger.info(
            "[#%d] RAW response (first 1500 chars): %r",
            req_id, response_text[:1500],
        )

    completion_tokens = estimate_tokens(response_text)

    if is_tool_request:
        parsed = tool_emulation.parse_tool_calls_from_response(response_text)
    else:
        parsed = {"type": "text", "content": response_text}

    proxy_stats.record_success(elapsed, prompt_tokens, completion_tokens)

    if parsed["type"] == "tool_calls":
        tool_calls = parsed["tool_calls"]
        tool_names = [tc["tool_name"] for tc in tool_calls]
        proxy_stats.record_tool_emulated()
        loop_detector.record_tool_calls(conv_id, tool_calls)

        logger.info(
            "[#%d] Tool call emulated: %s (%.1fs, ~%d tokens)",
            req_id, tool_names, elapsed, completion_tokens,
        )

        if use_stream:
            return StreamingResponse(
                stream_tool_calls_response(tool_calls, model),
                media_type="text/event-stream",
            )

        completion_id = generate_completion_id()
        response_body = tool_emulation.build_tool_calls_response(
            tool_calls, model, completion_id
        )
        response_body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        return JSONResponse(content=response_body)

    logger.info(
        "[#%d] Response: ~%d tokens in %.1fs (session: ~%s)",
        req_id, completion_tokens, elapsed,
        _format_tokens(proxy_stats.total_tokens),
    )

    if use_stream:
        return StreamingResponse(
            stream_response(parsed["content"], model),
            media_type="text/event-stream",
        )
    return JSONResponse(content={
        "id": generate_completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant",
                                 "content": parsed["content"]},
                     "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


@app.post("/v1")
async def proxy_chat_alt(request: Request):
    return await proxy_chat(request)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": DEFAULT_MODEL, "object": "model",
                  "created": 0, "owned_by": "apifreellm"}],
    }


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/v1/debug/tools")
async def debug_tools():
    result = []
    for key, prompt in tool_emulation._tools_prompt_cache.items():
        tokens = estimate_tokens(prompt)
        tool_sizes = {}
        for tool_name in sorted(key):
            marker = f"TOOL: {tool_name}\n"
            start = prompt.find(marker)
            if start == -1:
                continue
            next_tool = prompt.find("\nTOOL: ", start + 1)
            if next_tool == -1:
                end = prompt.find("\n\nHOW TO CALL", start)
                if end == -1:
                    end = start + 500
            else:
                end = next_tool
            block = prompt[start:end]
            tool_sizes[tool_name] = {
                "chars": len(block),
                "tokens": estimate_tokens(block),
                "first_150": block[:150],
            }
        result.append({
            "tools_count": len(key),
            "prompt_chars": len(prompt),
            "prompt_tokens": tokens,
            "tool_sizes": tool_sizes,
        })
    return {"cache_entries": len(result), "entries": result}


@app.get("/v1/debug/loops")
async def debug_loops():
    sessions_info = []
    now = time.monotonic()
    for conv_id, session in loop_detector._sessions.items():
        age = now - session.last_active
        sessions_info.append({
            "conversation_id": conv_id,
            "age_seconds": round(age, 1),
            "call_counts": session.call_counts,
            "pressure_count": session.pressure_count,
            "unique_arg_sets": {
                k: len(v) for k, v in session.call_arg_hashes.items()
            },
        })
    return {
        "active_sessions": len(sessions_info),
        "loop_detection_threshold": LOOP_DETECTION_THRESHOLD,
        "session_ttl_seconds": LOOP_SESSION_TTL,
        "sessions": sessions_info,
    }


if __name__ == "__main__":
    from config import load_config
    config = load_config()
    uvicorn.run(
        "main:app",
        host=config["server"]["host"],
        port=config["server"]["port"],
        log_level="info",
        access_log=False,
    )
