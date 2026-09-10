"""Финальная короткая рекомендация ИИ поверх уже сгруппированных и
отфильтрованных карточек моделей (см. grouping.py) — какую модель взять с
учётом критериев пользователя. Цену и "дешевле всего" внутри модели уже
считает код в compare.py, ИИ сюда не лезет — сравнивает только модели между
собой по характеристикам, чтобы не повторился баг с выдуманным "самым
дешёвым" по вперемешку разным товарам."""
import logging

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL
from models import ProductGroup

log = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM_PROMPT = (
    "Тебе дают запрос и критерии пользователя, и уже готовые карточки моделей "
    "товара — каждая строка ниже одна модель, для неё уже посчитана "
    "минимальная цена среди магазинов (карточки уже отфильтрованы по "
    "релевантности и сгруппированы, это не вперемешку разные товары). НЕ "
    "называй другую цену и не переопределяй, какая модель дешевле — только "
    "используй то, что дано.\n\n"
    "Если карточка одна — коротко скажи, подходит ли она под критерии "
    "пользователя (или просто в чём её сильные стороны, если критериев нет). "
    "Если карточек несколько — посоветуй, какую взять, ориентируясь в первую "
    "очередь на критерии пользователя и характеристики, а не на цену (цену "
    "пользователь и так видит в карточке).\n\n"
    "Ответь 1-2 короткими предложениями на русском, *звёздочки* для "
    "названия модели (Telegram Markdown), без вступлений и повторения цен. "
    "Если реально нечего посоветовать — ответь пустой строкой, без пояснений."
)


def _describe(g: ProductGroup) -> str:
    cheapest = g.products[0]
    price = f"{cheapest.price} {cheapest.currency}"
    line = f"{g.model}"
    if g.specs:
        line += f" ({g.specs})"
    line += f" — от {price}"
    return line


async def recommend(query: str, criteria: str, groups: list[ProductGroup]) -> str | None:
    if not OPENAI_API_KEY or not groups:
        return None
    if len(groups) == 1 and not criteria:
        return None  # одна модель и пользователь ничего конкретного не просил — советовать нечего

    listing = "\n".join(_describe(g) for g in groups)
    user_prompt = f'Запрос пользователя: "{query}"\n'
    if criteria:
        user_prompt += f"Важные для пользователя критерии: {criteria}\n"
    user_prompt += f"\nНайденные модели:\n{listing}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_completion_tokens": 300,
                    # luna — reasoning-модель: без этого тратит весь лимит токенов
                    # на скрытые размышления и отдаёт пустой ответ (см. grouping.py).
                    "reasoning_effort": "none",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return content or None
    except Exception:
        log.exception("advice: не удалось получить рекомендацию через ИИ")
        return None
