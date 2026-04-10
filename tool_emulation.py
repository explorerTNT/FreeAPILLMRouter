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

logger = logging.getLogger(__name__)


# ====== КОНСТАНТЫ ======

# Максимальное количество инструментов, описания которых включаем
# в промпт. Claude Code передаёт ~20-30 инструментов.
MAX_TOOLS_IN_PROMPT = 25

# Инструменты Claude Code, которые используются чаще всего.
# Они получают приоритет при сортировке перед включением в промпт.
HIGH_PRIORITY_TOOLS = {
    "Read", "Write", "Edit", "Bash", "ListDir",
    "Search", "Grep", "MultiEdit", "TodoRead", "TodoWrite",
}

# Кеш для tools_prompt — набор инструментов Claude Code не меняется
# между запросами, поэтому незачем генерировать промпт каждый раз.
# Ключ — frozenset имён инструментов, значение — готовый промпт.
_tools_prompt_cache: dict[frozenset, str] = {}


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


def build_tools_system_prompt(tools: list[dict]) -> str:
    if not tools:
        return ""

    tool_names = frozenset(
        t.get("function", {}).get("name", "")
        for t in tools
        if t.get("type") == "function"
    )

    if tool_names in _tools_prompt_cache:
        return _tools_prompt_cache[tool_names]

    prompt = _generate_tools_prompt(tools)
    _tools_prompt_cache[tool_names] = prompt

    if len(_tools_prompt_cache) > 10:
        oldest_key = next(iter(_tools_prompt_cache))
        del _tools_prompt_cache[oldest_key]

    return prompt


def _generate_tools_prompt(tools: list[dict]) -> str:
    sorted_tools = _sort_tools_by_priority(tools)
    selected_tools = sorted_tools[:MAX_TOOLS_IN_PROMPT]
    skipped_count = len(sorted_tools) - len(selected_tools)

    tool_descriptions = []
    for tool in selected_tools:
        desc = _format_tool_description(tool)
        if desc:
            tool_descriptions.append(desc)

    if not tool_descriptions:
        return ""

    tools_block = "\n\n".join(tool_descriptions)

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
    format_template = json.dumps(
        [{"tool_name": "<name>", "tool_call_id": "<unique_id>",
          "arguments": {"<param>": "<value>"}}],
        ensure_ascii=False, indent=2,
    )
    # Пример Write — добавлен потому что модель знала что делать,
    # но писала код в чат вместо того чтобы сохранить через инструмент.
    # Конкретный пример устраняет эту неоднозначность.
    example_write = json.dumps(
        [{"tool_name": "Write", "tool_call_id": "call_001",
          "arguments": {
              "file_path": "/path/to/File.cs",
              "content": "// full updated file content here\npublic class Example {}"
          }}],
        ensure_ascii=False, indent=2,
    )
    # Пример Edit — точечное изменение без перезаписи всего файла
    example_edit = json.dumps(
        [{"tool_name": "Edit", "tool_call_id": "call_001",
          "arguments": {
              "file_path": "/path/to/File.cs",
              "old_string": "// old code here",
              "new_string": "// new fixed code here"
          }}],
        ensure_ascii=False, indent=2,
    )

    prompt = (
        "[TOOL CALLING MODE]\n"
        "You have access to tools listed below. Your job is to decide "
        "whether the current task requires a tool call or a plain text "
        "response.\n\n"
        #
        # Правило ориентации — модель раньше угадывала расширения файлов
        # (.js/.ts вместо .cs) и ничего не находила. Теперь требуем
        # сначала осмотреть директорию.
        #
        "PROJECT ORIENTATION RULE:\n"
        "Before searching for anything, FIRST use ListDir or Glob to "
        "inspect the current directory. Do NOT assume the programming "
        "language, file extensions, or framework — discover them from "
        "the actual files present.\n\n"
        "AVAILABLE TOOLS:\n"
        + tools_block + "\n\n"
        "HOW TO CALL A TOOL:\n"
        "Respond with ONLY a JSON code block — NO text before or after:\n"
        "```json\n"
        + format_template + "\n"
        "```\n\n"
        "EXAMPLE — reading a file:\n"
        "User says: \"Read the file main.py\"\n"
        "You respond:\n"
        "```json\n"
        + example_single + "\n"
        "```\n\n"
        "EXAMPLE — calling multiple tools at once:\n"
        "User says: \"Read config.py and list the src directory\"\n"
        "You respond:\n"
        "```json\n"
        + example_multi + "\n"
        "```\n\n"
        #
        # Примеры Write и Edit — КЛЮЧЕВОЕ добавление.
        # Без них модель понимала что нужно сделать, но писала код
        # в чат как csharp-блок вместо вызова Write/Edit инструмента.
        #
        "EXAMPLE — saving code changes to a file (Write tool):\n"
        "You analyzed the code and know what to fix. "
        "You MUST save changes using Write or Edit — "
        "NEVER show the final code in plain text:\n"
        "```json\n"
        + example_write + "\n"
        "```\n\n"
        "EXAMPLE — editing part of a file (Edit tool):\n"
        "You want to change only a specific section of a file:\n"
        "```json\n"
        + example_edit + "\n"
        "```\n\n"
        "EXAMPLE — plain text response (NO tool needed):\n"
        "User says: \"Explain what a decorator is in Python\"\n"
        "You respond with a normal text explanation. "
        "Do NOT wrap it in JSON.\n\n"
        "EXAMPLE — after receiving a tool result:\n"
        "You called Read on main.py and got the file contents. Now:\n"
        "- If you need more info, call another tool.\n"
        "- If you have enough info, apply the fix using Write or Edit.\n"
        "- NEVER show the fixed code in plain text — use Write or Edit.\n"
        "- NEVER repeat a tool call you already made with the same "
        "arguments.\n\n"
        "STRICT RULES:\n"
        "1. tool_call_id must be unique: \"call_001\", \"call_002\", etc.\n"
        "2. ONLY use tools from the list above — never invent tool names.\n"
        "3. Arguments must match the parameter types exactly.\n"
        "4. If no tool fits the task, respond with plain text — "
        "do NOT force a tool call.\n"
        #
        # Правило 5 усилено: явно запрещаем показывать готовый код текстом.
        # Модель должна понять что Write/Edit — это единственный способ
        # применить изменения, а не просто опция.
        #
        "5. !! CRITICAL !! When calling a tool: output ONLY the ```json "
        "block. Zero words before it, zero words after it.\n"
        "   This applies to Write and Edit too — to save code changes "
        "you MUST call Write or Edit tool. NEVER output the fixed code "
        "as plain text or in a ```csharp / ```python block.\n"
        "6. When you respond with plain text, NEVER use the JSON "
        "tool call format.\n"
        #
        # Правило 7 — для Edit с неверным old_string.
        # Модель выдумывала old_string из памяти и Edit падал.
        #
        "7. !! CRITICAL for Edit tool !! If Edit returns 'string not "
        "found': you MUST use Read to re-read the file first, then copy "
        "the EXACT text from the file as old_string. NEVER write "
        "old_string from memory — always copy from the actual file "
        "content you received.\n"
        "8. If a tool result is an error, analyze it and try a different "
        "approach.\n"
        "9. NEVER assume file extensions or programming language. Use "
        "ListDir or Glob first to discover the actual project structure.\n"
        #
        # Правило 10 — явный запрет на псевдокод и заглушки.
        # Без него модель иногда писала "// existing code..." в Write.
        #
        "10. When using Write tool: provide the COMPLETE file content. "
        "Never use placeholders like '// existing code...' or "
        "'// ... rest of file'. Write the entire file."
    )

    if skipped_count > 0:
        prompt += (
            f"\n\nNote: {skipped_count} less common tools were omitted "
            f"to save context space."
        )

    return prompt


def _sort_tools_by_priority(tools: list[dict]) -> list[dict]:
    def priority_key(tool: dict) -> int:
        name = tool.get("function", {}).get("name", "")
        return 0 if name in HIGH_PRIORITY_TOOLS else 1
    return sorted(tools, key=priority_key)


def _format_tool_description(tool: dict) -> str:
    if tool.get("type") != "function":
        return ""
    func = tool.get("function", {})
    name = func.get("name", "unknown")
    description = func.get("description", "No description")
    parameters = func.get("parameters", {})

    lines = [f"TOOL: {name}", f"Description: {description}"]
    props = parameters.get("properties", {})
    required_params = set(parameters.get("required", []))

    if props:
        lines.append("Parameters:")
        for param_name, param_info in props.items():
            param_type = param_info.get("type", "any")
            param_desc = param_info.get("description", "")
            is_required = (
                "required" if param_name in required_params else "optional"
            )
            enum_values = param_info.get("enum")
            type_str = param_type
            if enum_values:
                type_str = f"enum[{', '.join(str(v) for v in enum_values)}]"
            lines.append(
                f"  - {param_name} ({type_str}, {is_required}): {param_desc}"
            )
    else:
        lines.append("Parameters: none")

    return "\n".join(lines)


def convert_tool_messages_to_text(messages: list[dict]) -> list[dict]:
    converted = []
    for msg in messages:
        role = msg.get("role", "user")

        if role == "tool":
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
            tool_calls = msg["tool_calls"]
            calls_text = _format_assistant_tool_calls(tool_calls)
            text_content = msg.get("content", "") or ""
            full_content = text_content
            if calls_text:
                if full_content:
                    full_content += "\n\n"
                full_content += calls_text
            converted.append({"role": "assistant", "content": full_content})

        else:
            converted.append(msg)

    return converted


def _format_assistant_tool_calls(tool_calls: list[dict]) -> str:
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


def _try_json_parse(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def parse_tool_calls_from_response(response_text: str) -> dict:
    """
    Анализирует ответ модели — ищет вызовы инструментов.

    Стратегии (от строгой к мягкой):
    1. JSON в блоке ```json...``` — закрытом ИЛИ незакрытом
    2. Голый JSON массив [...]
    3. Одиночный JSON объект {...}
    """
    if not response_text or not response_text.strip():
        return {"type": "text", "content": response_text or ""}

    text = response_text.strip()

    tool_calls = _try_parse_json_code_block(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    tool_calls = _try_parse_raw_json_array(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    tool_calls = _try_parse_single_json_object(text)
    if tool_calls:
        return {"type": "tool_calls", "tool_calls": tool_calls}

    logger.info(
        "parse_tool_calls: no tool call pattern found, "
        "treating as plain text (first 100: %r)",
        text[:100],
    )
    return {"type": "text", "content": response_text}


def _try_parse_json_code_block(text: str) -> list[dict] | None:
    """
    Ищет ```json...``` блок в любом месте текста.

    Обрабатывает два случая:
    1. Закрытый блок: ```json\\n[...]\\n```  — нормальный ответ
    2. Незакрытый блок: ```json\\n[...]     — apifreellm обрезал ответ
    """
    # Сначала закрытый блок (нормальный случай)
    closed_pattern = r"```(?:json)?\s*\n?(.*?)```"
    closed_matches = re.findall(closed_pattern, text, re.DOTALL)

    for match in closed_matches:
        result = _validate_tool_calls_json(match.strip())
        if result:
            logger.debug("_try_parse_json_code_block: found closed block")
            return result

    # Незакрытый блок — apifreellm иногда обрезает ответ без закрывающих ```
    open_pattern = r"```(?:json)?\s*\n?(.*?)$"
    open_match = re.search(open_pattern, text, re.DOTALL)

    if open_match:
        content = open_match.group(1).strip()

        # Пробуем как есть
        result = _validate_tool_calls_json(content)
        if result:
            logger.info(
                "_try_parse_json_code_block: found unclosed block "
                "(response was truncated by upstream)"
            )
            return result

        # JSON обрезан на середине — пробуем починить
        fixed = _try_fix_truncated_json(content)
        if fixed:
            result = _validate_tool_calls_json(fixed)
            if result:
                logger.info(
                    "_try_parse_json_code_block: fixed truncated JSON"
                )
                return result

    return None


def _try_fix_truncated_json(text: str) -> str | None:
    """
    Чинит обрезанный JSON добавляя закрывающие скобки.

    Пример входа:  '[{"tool_name": "Read", "arguments": {"file_path": "/some/fil'
    Пример выхода: '[{"tool_name": "Read", "arguments": {"file_path": "/some/fil"}}]'

    Алгоритм: проходим по тексту учитывая строки (чтобы { внутри
    "строки" не считался скобкой), строим стек незакрытых скобок,
    добавляем недостающие закрывающие в конец.
    """
    text = text.rstrip()

    stack = []
    in_string = False
    escape_next = False

    for char in text:
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in '{[':
            stack.append(char)
        elif char == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif char == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    if not stack:
        return text  # уже сбалансирован

    closing = ""
    for bracket in reversed(stack):
        closing += '}' if bracket == '{' else ']'

    # Если обрезан внутри строки — сначала закрываем строку
    if in_string:
        return text + '"' + closing
    return text + closing


def _try_parse_raw_json_array(text: str) -> list[dict] | None:
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
    return _validate_tool_calls_json(stripped[:json_end])


def _try_parse_single_json_object(text: str) -> list[dict] | None:
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
    try:
        obj = json.loads(stripped[:json_end])
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "tool_name" in obj:
        return _validate_tool_calls_json(json.dumps([obj]))
    return None


def _validate_tool_calls_json(json_str: str) -> list[dict] | None:
    data = _try_json_parse(json_str)

    # Очистка двойных скобок — артефакт копирования из примеров промпта
    if data is None and ("{{" in json_str or "}}" in json_str):
        cleaned = json_str.replace("{{", "{").replace("}}", "}")
        data = _try_json_parse(cleaned)
        if data is not None:
            logger.info("Fixed double-braces in model response")

    if data is None or not isinstance(data, list) or len(data) == 0:
        return None

    validated_calls = []
    for item in data:
        if not isinstance(item, dict) or "tool_name" not in item:
            continue
        call = {
            "tool_name": str(item["tool_name"]),
            "tool_call_id": str(
                item.get("tool_call_id", f"call_{uuid.uuid4().hex[:8]}")
            ),
            "arguments": item.get("arguments", {}),
        }
        if not isinstance(call["arguments"], dict):
            try:
                call["arguments"] = json.loads(str(call["arguments"]))
            except (json.JSONDecodeError, TypeError):
                call["arguments"] = {}
        validated_calls.append(call)

    return validated_calls if validated_calls else None


def build_tool_calls_response(
    tool_calls: list[dict], model: str, completion_id: str
) -> dict:
    import time
    openai_tool_calls = []
    for tc in tool_calls:
        openai_tool_calls.append({
            "id": tc["tool_call_id"],
            "type": "function",
            "function": {
                "name": tc["tool_name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
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
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_tool_calls_stream_events(
    tool_calls: list[dict], model: str, completion_id: str
) -> list[str]:
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
            "delta": {"role": "assistant", "content": None, "tool_calls": []},
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
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        })
    events.append(f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n")

    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    events.append(f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n")
    events.append("data: [DONE]\n\n")
    return events
