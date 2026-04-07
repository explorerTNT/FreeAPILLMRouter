"""
Модуль эмуляции tool calling для моделей без нативной поддержки.

Принцип работы:
1. Получаем запрос с tools[] от клиента (CCR / Claude Code)
2. Конвертируем описания инструментов в текстовую инструкцию в промпте
3. Отправляем обычный текстовый запрос в ApiFreeLLM
4. Парсим ответ модели — ищем JSON-блоки с вызовами инструментов
5. Оборачиваем найденные вызовы в формат OpenAI tool_calls

Это позволяет использовать Claude Code (через CCR) с любым текстовым API,
даже если он не поддерживает function calling нативно.

Обычные запросы (без tools) проходят мимо этого модуля без изменений.
"""

import json
import re
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ====== КОНСТАНТЫ ======

# Максимальное количество инструментов, описания которых включаем
# в промпт. Claude Code передаёт ~20-30 инструментов.
MAX_TOOLS_IN_PROMPT = 25

# Инструменты Claude Code, которые используются чаще всего.
# Они получают приоритет при сортировке перед включением в промпт.
HIGH_PRIORITY_TOOLS = {
    "Read",           # чтение файлов — самый частый
    "Write",          # запись файлов
    "Edit",           # редактирование файлов
    "Bash",           # выполнение команд
    "ListDir",        # просмотр директорий
    "Search",         # поиск по файлам
    "Grep",           # grep по коду
    "MultiEdit",      # множественное редактирование
    "TodoRead",       # чтение списка задач
    "TodoWrite",      # запись в список задач
}

# Кеш для tools_prompt — набор инструментов Claude Code не меняется
# между запросами, поэтому незачем генерировать промпт каждый раз.
# Ключ — frozenset имён инструментов, значение — готовый промпт.
_tools_prompt_cache: dict[frozenset, str] = {}


# ====== ОПРЕДЕЛЕНИЕ РЕЖИМА ======

def has_tools(request_data: dict) -> bool:
    """
    Проверяет, содержит ли запрос определения инструментов.
    Если да — нужно активировать эмуляцию tool calling.
    Если нет — это обычный чатовый запрос, пропускаем как есть.
    """
    tools = request_data.get("tools")
    return bool(tools) and isinstance(tools, list) and len(tools) > 0


def has_tool_results(messages: list[dict]) -> bool:
    """
    Проверяет, содержит ли история сообщений результаты вызова
    инструментов. Роль "tool" в OpenAI формате = результат
    выполнения инструмента.
    """
    return any(msg.get("role") == "tool" for msg in messages)


# ====== ГЕНЕРАЦИЯ СИСТЕМНОГО ПРОМПТА ДЛЯ ИНСТРУМЕНТОВ ======

def build_tools_system_prompt(tools: list[dict]) -> str:
    """
    Конвертирует массив tools[] из OpenAI формата в текстовую
    инструкцию.

    Использует кеш: если набор инструментов не изменился,
    возвращает ранее сгенерированный промпт.

    Args:
        tools: массив инструментов в формате OpenAI

    Returns:
        Текстовая инструкция для вставки в system prompt.
    """
    if not tools:
        return ""

    # Проверяем кеш по набору имён инструментов
    tool_names = frozenset(
        t.get("function", {}).get("name", "")
        for t in tools
        if t.get("type") == "function"
    )

    if tool_names in _tools_prompt_cache:
        return _tools_prompt_cache[tool_names]

    # Кеш-промах — генерируем заново
    prompt = _generate_tools_prompt(tools)
    _tools_prompt_cache[tool_names] = prompt

    # Ограничиваем размер кеша
    if len(_tools_prompt_cache) > 10:
        oldest_key = next(iter(_tools_prompt_cache))
        del _tools_prompt_cache[oldest_key]

    return prompt


def _generate_tools_prompt(tools: list[dict]) -> str:
    """
    Строит полный промпт с описаниями инструментов.
    Включает примеры и строгие правила для надёжности.

    ВАЖНО: примеры формируются через json.dumps() а не через f-string,
    чтобы модель видела нормальные { } а не экранированные {{ }}.
    Модель копирует формат примеров буквально — если она увидит
    {{ }} то вернёт {{ }}, и парсер не распознает JSON.
    """
    # Сортируем: приоритетные инструменты первыми
    sorted_tools = _sort_tools_by_priority(tools)

    # Ограничиваем количество
    selected_tools = sorted_tools[:MAX_TOOLS_IN_PROMPT]
    skipped_count = len(sorted_tools) - len(selected_tools)

    # Формируем описание каждого инструмента
    tool_descriptions = []
    for tool in selected_tools:
        desc = _format_tool_description(tool)
        if desc:
            tool_descriptions.append(desc)

    if not tool_descriptions:
        return ""

    tools_block = "\n\n".join(tool_descriptions)

    # Примеры формируем через json.dumps чтобы в итоговом тексте
    # были нормальные { }, а не {{ }} из f-string экранирования.
    example_single = json.dumps(
        [{"tool_name": "Read", "tool_call_id": "call_001",
          "arguments": {"file_path": "main.py"}}],
        ensure_ascii=False, indent=2,
    )

    example_multi = json.dumps(
        [
            {"tool_name": "Read", "tool_call_id": "call_001",
             "arguments": {"file_path": "config.py"}},
            {"tool_name": "ListDir", "tool_call_id": "call_002",
             "arguments": {"path": "src"}},
        ],
        ensure_ascii=False, indent=2,
    )

    # Шаблон формата вызова — тоже через json.dumps
    format_template = json.dumps(
        [{"tool_name": "<name>", "tool_call_id": "<unique_id>",
          "arguments": {"<param>": "<value>"}}],
        ensure_ascii=False, indent=2,
    )

    prompt = (
        "[TOOL CALLING MODE]\n"
        "You have access to tools listed below. Your job is to decide "
        "whether the current task requires a tool call or a plain text "
        "response.\n\n"
        #
        # --- Список инструментов ---
        #
        "AVAILABLE TOOLS:\n"
        + tools_block + "\n\n"
        #
        # --- Формат вызова ---
        #
        "HOW TO CALL A TOOL:\n"
        "Respond with ONLY a JSON code block in this exact format — "
        "no text before or after it:\n"
        "```json\n"
        + format_template + "\n"
        "```\n\n"
        #
        # --- Пример: один вызов ---
        #
        "EXAMPLE — calling a tool:\n"
        "User says: \"Read the file main.py\"\n"
        "You respond:\n"
        "```json\n"
        + example_single + "\n"
        "```\n\n"
        #
        # --- Пример: несколько вызовов ---
        #
        "EXAMPLE — calling multiple tools at once:\n"
        "User says: \"Read config.py and list the src directory\"\n"
        "You respond:\n"
        "```json\n"
        + example_multi + "\n"
        "```\n\n"
        #
        # --- Пример: когда НЕ вызывать ---
        #
        "EXAMPLE — plain text response (NO tool needed):\n"
        "User says: \"Explain what a decorator is in Python\"\n"
        "You respond with a normal text explanation. "
        "Do NOT wrap it in JSON.\n\n"
        #
        # --- Пример: после получения результата ---
        #
        "EXAMPLE — after receiving a tool result:\n"
        "You called Read on main.py and got the file contents. Now:\n"
        "- If you need more info, call another tool.\n"
        "- If you have enough info, respond with plain text.\n"
        "- NEVER repeat a tool call you already made with the same "
        "arguments.\n\n"
        #
        # --- Строгие правила ---
        #
        "STRICT RULES:\n"
        "1. tool_call_id must be unique: \"call_001\", \"call_002\", "
        "etc.\n"
        "2. ONLY use tools from the list above — never invent tool "
        "names.\n"
        "3. Arguments must match the parameter types exactly.\n"
        "4. If no tool fits the task, respond with plain text — "
        "do NOT force a tool call.\n"
        "5. NEVER put text before or after the JSON code block "
        "when calling a tool.\n"
        "6. When you respond with plain text, NEVER use the JSON "
        "tool call format.\n"
        "7. If a tool result is an error, analyze it and either try "
        "a different approach or explain the error to the user."
    )

    if skipped_count > 0:
        prompt += (
            f"\n\nNote: {skipped_count} less common tools were omitted "
            f"to save context space."
        )

    return prompt


def _sort_tools_by_priority(tools: list[dict]) -> list[dict]:
    """
    Сортирует инструменты: часто используемые в Claude Code —
    первыми.
    """
    def priority_key(tool: dict) -> int:
        name = tool.get("function", {}).get("name", "")
        return 0 if name in HIGH_PRIORITY_TOOLS else 1

    return sorted(tools, key=priority_key)


def _format_tool_description(tool: dict) -> str:
    """
    Форматирует один инструмент в читаемое текстовое описание.
    """
    if tool.get("type") != "function":
        return ""

    func = tool.get("function", {})
    name = func.get("name", "unknown")
    description = func.get("description", "No description")
    parameters = func.get("parameters", {})

    lines = [f"TOOL: {name}"]
    lines.append(f"Description: {description}")

    props = parameters.get("properties", {})
    required_params = set(parameters.get("required", []))

    if props:
        lines.append("Parameters:")
        for param_name, param_info in props.items():
            param_type = param_info.get("type", "any")
            param_desc = param_info.get("description", "")
            is_required = (
                "required" if param_name in required_params
                else "optional"
            )

            enum_values = param_info.get("enum")
            type_str = param_type
            if enum_values:
                type_str = (
                    f"enum[{', '.join(str(v) for v in enum_values)}]"
                )

            lines.append(
                f"  - {param_name} ({type_str}, {is_required}): "
                f"{param_desc}"
            )
    else:
        lines.append("Parameters: none")

    return "\n".join(lines)


# ====== КОНВЕРТАЦИЯ ИСТОРИИ С TOOL RESULTS ======

def convert_tool_messages_to_text(messages: list[dict]) -> list[dict]:
    """
    Конвертирует сообщения с ролью "tool" (результаты инструментов)
    в обычные текстовые сообщения, понятные модели.

    Результаты инструментов передаются КАК ЕСТЬ, без обрезки.
    Если контекст переполняется — trim_messages_to_fit в main.py
    уберёт старые сообщения, а не обрежет содержимое свежих.

    Args:
        messages: список сообщений в OpenAI формате

    Returns:
        Новый список без ролей "tool", с текстовыми эквивалентами.
    """
    converted = []

    for msg in messages:
        role = msg.get("role", "user")

        if role == "tool":
            # Результат инструмента -> текстовое сообщение от "user"
            tool_call_id = msg.get("tool_call_id", "unknown")
            content = msg.get("content", "")

            converted.append({
                "role": "user",
                "content": (
                    f"[Tool Result for {tool_call_id}]\n"
                    f"<result>\n{content}\n</result>"
                ),
            })

        elif role == "assistant" and msg.get("tool_calls"):
            # Ответ модели с tool_calls -> текстовый JSON
            tool_calls = msg["tool_calls"]
            calls_text = _format_assistant_tool_calls(tool_calls)

            text_content = msg.get("content", "") or ""
            full_content = text_content
            if calls_text:
                if full_content:
                    full_content += "\n\n"
                full_content += calls_text

            converted.append({
                "role": "assistant",
                "content": full_content,
            })

        else:
            # Обычное сообщение — оставляем как есть
            converted.append(msg)

    return converted


def _format_assistant_tool_calls(tool_calls: list[dict]) -> str:
    """
    Форматирует tool_calls из ответа assistant в текстовый JSON.
    """
    calls = []
    for tc in tool_calls:
        func = tc.get("function", {})
        call_id = tc.get("id", f"call_{uuid.uuid4().hex[:6]}")
        name = func.get("name", "unknown")

        args = func.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}

        calls.append({
            "tool_name": name,
            "tool_call_id": call_id,
            "arguments": args,
        })

    return (
        "```json\n"
        + json.dumps(calls, ensure_ascii=False, indent=2)
        + "\n```"
    )


# ====== БЕЗОПАСНЫЙ ПАРСИНГ JSON ======

def _try_json_parse(text: str):
    """
    Безопасный парсинг JSON — возвращает None при ошибке.
    Используется во всех функциях парсинга ответов модели.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


# ====== ПАРСИНГ ОТВЕТА МОДЕЛИ ======

def parse_tool_calls_from_response(response_text: str) -> dict:
    """
    Анализирует текстовый ответ модели и определяет:
    содержит ли он вызовы инструментов или это обычный текст.

    Использует 3 стратегии парсинга от строгой к мягкой:
    1. JSON в блоке ```json ... ``` (основной формат)
    2. Голый JSON массив [...] (модель забыла code block)
    3. Одиночный JSON объект {...} (модель забыла массив)

    Args:
        response_text: полный текст ответа от ApiFreeLLM

    Returns:
        dict с ключами:
        - "type": "tool_calls" | "text"
        - "tool_calls": [...] — если type == "tool_calls"
        - "content": "..." — если type == "text"
    """
    if not response_text or not response_text.strip():
        return {"type": "text", "content": response_text or ""}

    text = response_text.strip()

    # Стратегия 1: JSON в блоке ```json ... ```
    tool_calls = _try_parse_json_code_block(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    # Стратегия 2: Голый JSON массив (без code block)
    tool_calls = _try_parse_raw_json_array(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    # Стратегия 3: JSON объект (одиночный вызов)
    tool_calls = _try_parse_single_json_object(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    # Обычный текстовый ответ
    return {"type": "text", "content": response_text}


def _try_parse_json_code_block(text: str) -> list[dict] | None:
    """Ищет JSON в блоке ```json ... ```."""
    pattern = r"```(?:json)?\s*\n?(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)

    for match in matches:
        result = _validate_tool_calls_json(match.strip())
        if result:
            return result

    return None


def _try_parse_raw_json_array(text: str) -> list[dict] | None:
    """Ищет голый JSON массив без code block."""
    stripped = text.strip()
    if not stripped.startswith("["):
        return None

    bracket_depth = 0
    json_end = -1
    for i, char in enumerate(stripped):
        if char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                json_end = i + 1
                break

    if json_end == -1:
        return None

    json_str = stripped[:json_end]
    return _validate_tool_calls_json(json_str)


def _try_parse_single_json_object(text: str) -> list[dict] | None:
    """Ищет одиночный JSON объект с tool_name."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None

    brace_depth = 0
    json_end = -1
    for i, char in enumerate(stripped):
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
            if brace_depth == 0:
                json_end = i + 1
                break

    if json_end == -1:
        return None

    json_str = stripped[:json_end]
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    if isinstance(obj, dict) and "tool_name" in obj:
        return _validate_tool_calls_json(json.dumps([obj]))

    return None


def _validate_tool_calls_json(json_str: str) -> list[dict] | None:
    """
    Парсит и валидирует JSON как массив вызовов инструментов.

    Включает очистку от двойных скобок {{ }} — модель иногда
    копирует их из примеров в промпте (артефакт Python f-string
    экранирования). Парсер сначала пробует как есть, и только
    если не получилось — чистит скобки и пробует снова.

    Каждый элемент массива должен содержать:
    - tool_name (str) — имя инструмента
    - arguments (dict) — параметры вызова
    - tool_call_id (str, optional) — ID вызова

    Returns:
        Список валидных вызовов или None если формат неправильный.
    """
    # Первая попытка — парсим как есть
    data = _try_json_parse(json_str)

    # Если не получилось и есть двойные скобки — чистим и пробуем снова.
    # Модель может вернуть {{"key": "value"}} вместо {"key": "value"}
    # потому что увидела {{ }} в промпте (f-string escaping).
    if data is None and ("{{" in json_str or "}}" in json_str):
        cleaned = json_str.replace("{{", "{").replace("}}", "}")
        data = _try_json_parse(cleaned)
        if data is not None:
            logger.info("Fixed double-braces in model response")

    if data is None:
        return None

    if not isinstance(data, list):
        return None

    if len(data) == 0:
        return None

    validated_calls = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if "tool_name" not in item:
            continue

        call = {
            "tool_name": str(item["tool_name"]),
            "tool_call_id": str(
                item.get(
                    "tool_call_id",
                    f"call_{uuid.uuid4().hex[:8]}",
                )
            ),
            "arguments": item.get("arguments", {}),
        }

        # arguments должен быть словарём
        if not isinstance(call["arguments"], dict):
            try:
                call["arguments"] = json.loads(
                    str(call["arguments"])
                )
            except (json.JSONDecodeError, TypeError):
                call["arguments"] = {}

        validated_calls.append(call)

    return validated_calls if validated_calls else None


# ====== ФОРМИРОВАНИЕ ОТВЕТА В OPENAI ФОРМАТЕ ======

def build_tool_calls_response(
    tool_calls: list[dict],
    model: str,
    completion_id: str,
) -> dict:
    """
    Формирует ответ в формате OpenAI Chat Completions
    с tool_calls.
    """
    import time

    openai_tool_calls = []
    for tc in tool_calls:
        openai_tool_calls.append({
            "id": tc["tool_call_id"],
            "type": "function",
            "function": {
                "name": tc["tool_name"],
                "arguments": json.dumps(
                    tc["arguments"], ensure_ascii=False
                ),
            },
        })

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": openai_tool_calls,
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_tool_calls_stream_events(
    tool_calls: list[dict],
    model: str,
    completion_id: str,
) -> list[str]:
    """
    Формирует SSE-события для стримингового ответа
    с tool_calls.
    """
    import time
    created = int(time.time())

    events = []

    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            },
            "finish_reason": None,
        }],
    }

    for i, tc in enumerate(tool_calls):
        first_chunk["choices"][0]["delta"]["tool_calls"].append({
            "index": i,
            "id": tc["tool_call_id"],
            "type": "function",
            "function": {
                "name": tc["tool_name"],
                "arguments": json.dumps(
                    tc["arguments"], ensure_ascii=False
                ),
            },
        })

    events.append(
        f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"
    )

    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "tool_calls",
        }],
    }
    events.append(
        f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
    )
    events.append("data: [DONE]\n\n")

    return events
