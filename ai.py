"""Общий слой вызова OpenAI chat/completions для query_planner.py, grouping.py
и advice.py — раньше в каждом из них почти дословно повторялись httpx-клиент,
заголовок с ключом, тело запроса и разбор choices[0].message.content. Здесь
это сведено в две функции: ask_json (когда нужен response_format
json_object) и ask_text (обычный текстовый ответ).

Все ошибки (нет ключа, сетевой сбой, не-2xx, пустой или неразбираемый ответ)
гасятся здесь и логируются — исключения наружу не выходят. Вызывающий код во
всех трёх местах и так рассчитывает на запасной вариант при None/"" и не
должен оборачивать вызов в try/except."""
import json
import logging

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL

log = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"


async def _call(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    timeout: float,
    json_mode: bool,
) -> str | None:
    """Общий запрос к chat/completions. Возвращает сырой content из ответа
    или None, если ключа нет, запрос упал или ответ оказался пустым."""
    if not OPENAI_API_KEY:
        return None

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": max_tokens,
        # OPENAI_MODEL (gpt-5.6-luna) — reasoning-модель: без этого параметра
        # она тратит весь max_completion_tokens на скрытые размышления и
        # возвращает ПУСТОЙ ответ. Поймано вживую в query_planner.py и
        # grouping.py — обязателен в каждом запросе, не убирать.
        "reasoning_effort": "none",
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        log.exception("ai: запрос к OpenAI не удался")
        return None

    if not content or not content.strip():
        log.warning("ai: пустой ответ от OpenAI")
        return None
    return content


async def ask_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    timeout: float = 20,
) -> dict | None:
    """JSON-ответ ИИ (response_format=json_object). None — если нет ключа,
    запрос упал, ответ пустой или не разобрался как JSON-объект. Разбор
    конкретных полей — забота вызывающего кода (у каждого свой формат)."""
    content = await _call(
        system_prompt, user_prompt, max_tokens=max_tokens, timeout=timeout, json_mode=True
    )
    if content is None:
        return None
    try:
        parsed = json.loads(content)
    except Exception:
        log.exception("ai: ответ ИИ не разобрался как JSON")
        return None
    if not isinstance(parsed, dict):
        log.warning("ai: ответ ИИ — не JSON-объект")
        return None
    return parsed


async def ask_text(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    timeout: float = 20,
) -> str:
    """Обычный текстовый ответ ИИ. Пустая строка — если нет ключа, запрос
    упал или ответ пустой (вызывающий код трактует "" как "нечего сказать")."""
    content = await _call(
        system_prompt, user_prompt, max_tokens=max_tokens, timeout=timeout, json_mode=False
    )
    return content.strip() if content else ""
