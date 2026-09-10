"""Один батч-вызов ИИ на весь список найденных товаров: отсеивает то, что не
относится к искомой категории (наушники среди планшетов), и схлопывает
одинаковые модели из разных магазинов в группы — всё сравнение цен потом
идёт внутри группы, а не по всей плоской выдаче."""
import json
import logging

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL
from models import Product, ProductGroup

log = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM_PROMPT = (
    "Тебе дают категорию товара, который ищет пользователь, и пронумерованный "
    "список товаров, найденных в разных интернет-магазинах Казахстана по "
    "поисковому запросу (номер, магазин, название, цена, известные "
    "характеристики). Сделай три действия сразу:\n\n"
    "1) Отбрось номера, которые НЕ относятся к указанной категории товара "
    "(например, наушники, пылесос или чехол среди планшетов) — их не должно "
    "быть ни в одной группе.\n"
    "2) Среди оставшихся — сгруппируй номера, которые являются ОДНОЙ И ТОЙ ЖЕ "
    "моделью товара: совпадают бренд, модель, объём памяти/RAM+ROM, диагональ "
    "экрана, тип связи (LTE/Wi-Fi) — то есть покупатель, выбирая между ними, "
    "по сути выбирает магазин, а не разный товар. Это ОДНА группа, даже если "
    "у разных магазинов название написано по-разному (опечатки, порядок "
    "слов, разные обозначения единиц). Разные модификации (другой объём "
    "памяти, другая диагональ, LTE вместо Wi-Fi) — РАЗНЫЕ группы. Цвет "
    "(расцветка) НЕ входит в ось группировки — товары разных цветов одной и "
    "той же модели/объёма всё равно идут в ОДНУ группу.\n"
    "3) Для каждого номера внутри группы укажи variant — короткое название "
    "цвета/расцветки этого конкретного предложения, если оно понятно из "
    "названия (например «синий», «серый»), иначе пустая строка. Если в "
    "критериях пользователя явно назван нужный цвет — не включай в группу "
    "предложения других цветов вовсе (это не отдельная группа, просто "
    "нерелевантное предложение для этого запроса).\n\n"
    "Для каждой группы укажи: model — короткое каноническое название модели "
    "(бренд + модель + ключевая модификация типа памяти/диагонали, БЕЗ цвета "
    "и магазинных приписок), specs — 1-3 главные характеристики короткой "
    "строкой через запятую, тоже без цвета (без названий параметров, если "
    "они очевидны из контекста, например \"8/256GB, 11\\\", Wi-Fi\"). "
    "Отсортируй группы по релевантности запросу пользователя, самая "
    "релевантная — первая. Верни не больше 5 групп.\n\n"
    "Если по товару почти нет данных для определения модели — всё равно "
    "включи его отдельной группой, не выдумывай характеристики.\n\n"
    'Ответь строго JSON: {"groups": [{"model": "...", "specs": "...", '
    '"offers": [{"index": 1, "variant": "синий"}, {"index": 3, "variant": ""}]}]}'
)


def _describe(n: int, p: Product) -> str:
    specs = ", ".join(f"{k}: {v}" for k, v in p.specs.items() if v)
    price = f"{p.price} {p.currency}"
    line = f"{n}. [{p.shop}] {p.name} — {price}"
    if specs:
        line += f" (характеристики: {specs})"
    return line


def _fallback(products: list[Product]) -> list[ProductGroup]:
    """Без ИИ (нет ключа/ошибка) — не отсеиваем и не путаем группы, просто
    схлопываем буквально совпадающие названия, остальное — по одному товару."""
    groups: dict[str, list[Product]] = {}
    order: list[str] = []
    for p in products:
        key = p.name.strip().lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    result = []
    for key in order:
        items = sorted(groups[key], key=lambda p: p.price)
        result.append(ProductGroup(model=items[0].name, specs="", products=items))
    return result


async def group_and_filter(category: str, query: str, criteria: str, products: list[Product]) -> list[ProductGroup]:
    if not products:
        return []
    if not OPENAI_API_KEY:
        return _fallback(products)

    listing = "\n".join(_describe(n, p) for n, p in enumerate(products, start=1))
    user_prompt = f'Запрос пользователя: "{query}"\n'
    if category:
        user_prompt += f"Категория товара: {category}\n"
    if criteria:
        user_prompt += f"Важные для пользователя критерии: {criteria}\n"
    user_prompt += f"\nНайденные товары:\n{listing}"

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
                    "max_completion_tokens": 600,
                    # luna — reasoning-модель: без этого она тратит весь лимит
                    # токенов на скрытые размышления и отдаёт пустой ответ
                    # (та же проблема, что и в ai_specs, проверено вживую).
                    "reasoning_effort": "none",
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
        parsed = json.loads(data["choices"][0]["message"]["content"])
        raw_groups = parsed.get("groups")
        if not isinstance(raw_groups, list):
            raise ValueError("ответ ИИ без поля groups")
    except Exception:
        log.exception("grouping: не удалось сгруппировать через ИИ, показываю без группировки")
        return _fallback(products)

    by_index = {n: p for n, p in enumerate(products, start=1)}
    used: set[int] = set()
    result: list[ProductGroup] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        offers = [
            o for o in g.get("offers", [])
            if isinstance(o, dict) and isinstance(o.get("index"), int)
            and o["index"] in by_index and o["index"] not in used
        ]
        if not offers:
            continue
        used.update(o["index"] for o in offers)
        items = []
        for o in offers:
            p = by_index[o["index"]]
            variant = str(o.get("variant") or "").strip()
            p.variant = variant or None
            items.append(p)
        items.sort(key=lambda p: p.price)
        model = str(g.get("model") or items[0].name).strip()
        specs = str(g.get("specs") or "").strip()
        result.append(ProductGroup(model=model, specs=specs, products=items))
    return result
