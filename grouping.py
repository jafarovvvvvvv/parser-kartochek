"""Один батч-вызов ИИ на весь список найденных товаров: отсеивает то, что не
относится к искомой категории (наушники среди планшетов), и схлопывает
одинаковые модели из разных магазинов в группы — всё сравнение цен потом
идёт внутри группы, а не по всей плоской выдаче."""
import logging
import re

from ai import ask_json
from config import OPENAI_API_KEY
from models import Product, ProductGroup

log = logging.getLogger(__name__)

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

    parsed = await ask_json(_SYSTEM_PROMPT, user_prompt, max_tokens=600, timeout=20)
    raw_groups = parsed.get("groups") if parsed else None
    if not isinstance(raw_groups, list):
        log.warning("grouping: не удалось сгруппировать через ИИ, показываю без группировки")
        return _fallback(products)

    return _merge_same_model(_build_groups(raw_groups, products))


def _model_key(model: str) -> str:
    return re.sub(r"[^a-z0-9]", "", model.lower())


def _merge_same_model(groups: list[ProductGroup]) -> list[ProductGroup]:
    """ИИ иногда раскладывает одну и ту же модель по двум группам (например,
    предложения с микрофоном и без из одной линейки). Тогда «дешевле всего»
    считается внутри каждой половины, и пользователю показывается не самая
    низкая цена — поймано вживую на Sony MDR-ZX310AP: 18 670 с пометкой
    «дешевле всего» при 5 990 в соседней карточке."""
    merged: dict[str, ProductGroup] = {}
    order: list[str] = []
    for group in groups:
        key = _model_key(group.model)
        if key not in merged:
            merged[key] = group
            order.append(key)
            continue
        target = merged[key]
        target.products += group.products
        target.products.sort(key=lambda p: p.price)
        if not target.specs:
            target.specs = group.specs
    return [merged[key] for key in order]


def _build_groups(raw_groups: list, products: list[Product]) -> list[ProductGroup]:
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
