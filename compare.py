import tg
from models import Product, ProductGroup

SHOP_TITLES = {
    "wildberries": "Wildberries",
    "kaspi": "Kaspi.kz",
    "sulpak": "Sulpak",
    "technodom": "Технодом",
    "dns": "DNS",
}

# Карточка на модель, а не список из десятка позиций — иначе сравнение
# перекладывается на пользователя. Хватает 3, чтобы ответ оставался читаемым.
MAX_GROUPS = 3


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return few
    return many


def format_comparison(
    query: str,
    groups: list[ProductGroup],
    empty_shops: list[str],
    errored: set[str] = frozenset(),
) -> tuple[str, list[Product]]:
    """groups — уже отфильтрованные по релевантности и сгруппированные по
    модели (см. grouping.py). Возвращает (текст для Telegram, плоский список
    товаров в порядке нумерации в тексте — для кнопок «отслеживать»).

    Разметка HTML, а не Markdown: в названиях товаров и характеристиках
    попадаются * _ [ ], и как Markdown Telegram такое сообщение не принимает —
    экранировать в HTML надо только < > &."""
    lines = [f"🔎 <b>{tg.escape(query)}</b>", ""]
    flat: list[Product] = []

    shown = groups[:MAX_GROUPS]
    for group in shown:
        lines.append(f"<b>{tg.escape(group.model)}</b>")
        if group.specs:
            lines.append(f"<i>{tg.escape(group.specs)}</i>")
        multiple = len(group.products) > 1
        for i, p in enumerate(group.products):
            flat.append(p)
            n = len(flat)
            title = SHOP_TITLES.get(p.shop, p.shop)
            variant = f" ({tg.escape(p.variant)})" if p.variant else ""
            price = f"{p.price:,}".replace(",", " ")
            mark = " 💰 дешевле всего" if multiple and i == 0 else ""
            lines.append(
                f'{n}. <a href="{p.url}">{title}</a>{variant} — '
                f"<b>{price} {p.currency}</b>{mark}"
            )
        lines.append("")

    remaining = len(groups) - len(shown)
    if remaining > 0:
        word = plural(remaining, "модель", "модели", "моделей")
        lines.append(f"…и ещё {remaining} {word} — уточни запрос, если нужны именно они")
        lines.append("")

    for shop in empty_shops:
        title = SHOP_TITLES.get(shop, shop)
        lines.append(f"<b>{title}</b>: ничего подходящего не нашлось")
    for shop in errored:
        title = SHOP_TITLES.get(shop, shop)
        lines.append(f"<b>{title}</b>: не отвечает (ошибка запроса, не «нет товаров»)")

    if not shown and not empty_shops and not errored:
        lines.append(
            "Ничего не нашлось. Попробуй короче — одним-двумя словами, "
            "без цвета и объёма памяти: магазины плохо ищут по длинным фразам."
        )

    return "\n".join(lines).strip(), flat


def track_buttons(products: list[Product]) -> dict | None:
    """Кнопки «отслеживать» под карточками сравнения — чтобы не набирать
    /track N руками с телефона."""
    if not products:
        return None
    rows, row = [], []
    for n, p in enumerate(products[:8], start=1):
        row.append((f"📌 {n}", f"track:{n}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return tg.keyboard(rows)
