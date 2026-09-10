"""Превращает произвольный запрос пользователя в короткий поисковый запрос,
понятный поиску магазинов, плюс критерии для последующего ИИ-сравнения.
Если категория товара вообще не ясна — просит уточнить, вместо поиска."""
import json
import logging

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL

log = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM_PROMPT = (
    "Ты помогаешь превратить запрос пользователя в интернет-магазины Казахстана "
    "в короткий поисковый запрос (1-3 слова: категория товара + бренд/модель, "
    "если названы) — именно то, что вбивают в поиск на сайте магазина, а не "
    "полное предложение с прилагательными. Также выдели важные для пользователя "
    "критерии (по ним будем сравнивать и выбирать среди найденных товаров) — "
    "не включай их в сам поисковый запрос. Отдельно назови category — одно "
    "слово, категория товара в общем виде (например «планшет», «наушники», "
    "«кроссовки») — по нему потом отсеем мусор из выдачи магазинов.\n\n"
    "Если категория товара вообще не ясна (например «хочу подарок» без намёка "
    "на тип товара) — задай ОДИН короткий уточняющий вопрос вместо поиска. "
    "НЕ уточняй бюджет/бренд/модель, если категория и так понятна (например "
    "«планшет» или «планшет с хорошим экраном» — этого достаточно для поиска, "
    "остальное просто попадёт в критерии).\n\n"
    'Ответь строго JSON, один из двух форматов:\n'
    '{"action": "search", "query": "...", "category": "...", "criteria": "..."}\n'
    '{"action": "clarify", "question": "..."}'
)


async def plan(user_text: str, history: str = "") -> dict:
    fallback = {"action": "search", "query": user_text, "category": "", "criteria": ""}
    if not OPENAI_API_KEY:
        return fallback

    prompt = user_text if not history else f"{history}\nОтвет пользователя: {user_text}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_completion_tokens": 300,
                    "reasoning_effort": "none",
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
        parsed = json.loads(data["choices"][0]["message"]["content"])
        if parsed.get("action") == "search" and parsed.get("query"):
            return {
                "action": "search",
                "query": parsed["query"],
                "category": parsed.get("category", ""),
                "criteria": parsed.get("criteria", ""),
            }
        if parsed.get("action") == "clarify" and parsed.get("question"):
            return {"action": "clarify", "question": parsed["question"]}
    except Exception:
        log.exception("query_planner: не удалось спланировать запрос, ищем как есть")
    return fallback
