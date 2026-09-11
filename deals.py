"""Охота за уценкой: периодически обходит разделы уценённых товаров магазинов,
ловит экземпляры со скидкой выше порога и шлёт находку в Telegram — с дефектами
конкретного экземпляра, фото этих дефектов и ценой такого же нового товара в
других магазинах.

В отличие от scheduler.py (следит за ценой того, что пользователь сам выбрал),
здесь триггер — расписание и порог выгодности, а не запрос пользователя."""
import asyncio
import logging
import re

import db
import tg
from adapters import registry
from config import (
    DEALS_CHAT_ID,
    DEALS_MAX_PER_SCAN,
    DEALS_MIN_DISCOUNT,
    DEALS_SCAN_INTERVAL_MIN,
    OPENAI_API_KEY,
)
from compare import plural
from grouping import group_and_filter
from models import DiscountedUnit, Product
from relevance import rerank

log = logging.getLogger("deals")

RAW_LIMIT = 12
PER_SHOP = 3
PAUSE_BETWEEN_LOOKUPS_SEC = 4
# Больше в одно сообщение Telegram не влезет по длине, да и читать столько
# описаний дефектов подряд смысла нет.
MAX_UNITS_IN_MESSAGE = 8
# Названия в магазинах длинные и с повторами ("FreeBuds 5i T0014 Isle Blue
# FreeBuds 5i Isle Blue"), а поиск на длинных строках отдаёт пустоту — см.
# README про query_planner. Поэтому в чужие магазины уходит короткий запрос.
QUERY_WORDS = 5

# Обход тяжёлый (минуты) и пишет в seen_deals — два одновременных (плановый и
# запущенный руками) слали бы дубли и мешали друг другу.
_scan_lock = asyncio.Lock()


def _unique_words(text: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for word in text.split():
        key = word.lower().strip(",.;")
        if key and key not in seen:
            seen.add(key)
            words.append(word)
    return words


def clean_name(name: str) -> str:
    """Название без повторов. Магазины дублируют модель и цвет
    ("FreeBuds 5i T0014 Isle Blue FreeBuds 5i Isle Blue") — на таком названии
    ИИ хуже узнаёт товар в чужой выдаче и не собирает группу."""
    return " ".join(_unique_words(name))


def short_query(name: str) -> str:
    return " ".join(_unique_words(re.sub(r"\([^)]*\)", " ", name))[:QUERY_WORDS])


_MODEL_CODE_RE = re.compile(r"[a-z0-9][a-z0-9\-/]{3,}", re.I)

# Запчасти и аксессуары носят в названии артикул той же модели ("Подшипники для
# стиральной машины Haier HW80-B14979"), поэтому сравнение по артикулу считает их
# тем же товаром. Вживую это дало "такой же новый за 6 440 ₸" против уценённой
# стиралки за 124 995 и вывод "уценка не выгодна" — ровно та дезинформация,
# ради недопущения которой сравнение и делается.
_ACCESSORY_WORDS = (
    "подшипник", "шланг", "чехол", "фильтр", "ремень", "амортизатор", "помпа",
    "тэн", "щетк", "щётк", "манжет", "кабель", "адаптер", "зарядн", "крышк",
    "ручк", "стекло", "насос", "плат", "модуль", "запчаст", "деталь", "накладк",
    "наклейк", "пленк", "плёнк", "футляр", "кейс", "ремешок", "держател",
    "кронштейн", "переходник", "шнур", "уплотнител", "клапан", "патрубок",
    "амбушюр", "вкладыш", "элемент", "лампа", "картридж", "мешок", "насадк",
)
# Ниже этой доли от цены нового такого же товар почти наверняка не тот же самый,
# а его часть: даже самый дешёвый магазин так не отличается (для сравнения, самый
# выгодный честный аналог из пойманных вживую стоил 40% от цены Sulpak).
_IMPLAUSIBLE_PRICE_RATIO = 0.2


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def model_codes(name: str) -> list[str]:
    """Артикулы модели из названия («HW60-BP12929ASE», «MDR-EX15LP»): токены с
    буквами и цифрами разом. Именно они отличают модель от соседней в линейке."""
    codes = []
    for token in _MODEL_CODE_RE.findall(name):
        normalized = _normalize(token)
        if len(normalized) >= 5 and any(c.isdigit() for c in normalized) and any(
            c.isalpha() for c in normalized
        ):
            codes.append(normalized)
    return codes


def same_model(name: str, other: str) -> bool:
    """Точное совпадение модели без ИИ — по артикулу в названии. Строго: нет
    общего артикула — не считаем товар тем же самым (лучше промолчать, чем
    прислать «аналог», который на деле другая модель).

    Хвост до 3 символов прощаем: у одной модели это обычно код расцветки
    (MDR-EX15LP → MDR-EX15LPV), а цвет — атрибут предложения, не другая модель.
    Дальше расходятся уже разные модели линейки (HW60-BP12919 vs HW60-BP12929)."""
    ours = model_codes(name)
    theirs = model_codes(other)
    if not ours or not theirs:
        return False
    for code in ours:
        for other_code in theirs:
            if len(code) == len(other_code):
                if code == other_code:
                    return True
                continue
            longer, shorter = (code, other_code) if len(code) > len(other_code) else (other_code, code)
            if longer.startswith(shorter) and len(longer) - len(shorter) <= 3:
                return True
    return False


def looks_like_part(our_name: str, other_name: str) -> bool:
    """Похоже ли предложение на запчасть/аксессуар к нашему товару, а не на него сам."""
    other = other_name.lower()
    if any(word in other for word in _ACCESSORY_WORDS):
        return True
    # "X для стиральной машины Y" — предлог выдаёт принадлежность к другому товару.
    # Если предлог есть и в нашем названии ("Средство для очистки кофемашины"),
    # это уже свойство самого товара, а не признак запчасти.
    return " для " in other and " для " not in our_name.lower()


async def find_cheapest_analog(unit: DiscountedUnit) -> Product | None:
    """Самый дешёвый ТОЧНО такой же новый товар в других магазинах.

    Два независимых способа опознать «тот же самый товар», результаты
    объединяются: строгое совпадение по артикулу модели (работает без ИИ и почти
    не ошибается) и та же группировка ИИ, что и в обычном поиске — подкладываем
    уценённый товар в список кандидатов и берём то, что ИИ положил с ним в одну
    группу. Порознь каждый способ неполон: у Kaspi в названии нет артикула, а
    без ключа ИИ недоступен вовсе. Что не опознано — не предлагаем: лучше
    промолчать, чем прислать «аналог», который на деле другая модель."""
    # Свой же магазин из сравнения исключаем — там этот товар и лежит.
    rivals = [a for a in registry.SEARCH if a.name != unit.shop]
    query = short_query(unit.name)
    results = await asyncio.gather(
        *(a.search(query, limit=RAW_LIMIT) for a in rivals), return_exceptions=True
    )
    candidates: list[Product] = []
    for adapter, res in zip(rivals, results):
        if isinstance(res, Exception):
            log.warning("deals: магазин %s не ответил на %r: %r", adapter.name, query, res)
            continue
        candidates += rerank(unit.name, res, PER_SHOP)

    floor = unit.new_price * _IMPLAUSIBLE_PRICE_RATIO if unit.new_price else 0
    kept = []
    for p in candidates:
        if looks_like_part(unit.name, p.name):
            log.info("deals: %r отброшен как запчасть к %r", p.name, unit.name)
        elif p.price < floor:
            log.info("deals: %r за %s отброшен — слишком дёшево для %r", p.name, p.price, unit.name)
        else:
            kept.append(p)
    candidates = kept
    if not candidates:
        return None

    matched = [p for p in candidates if same_model(unit.name, p.name)]

    # Артикул есть не везде: Kaspi пишет "Наушники Huawei FreeBuds 5i чёрный"
    # без кода модели, и такой товар строгое сравнение пропустит. Поэтому,
    # когда ИИ доступен, добираем совпадения ещё и им.
    if OPENAI_API_KEY:
        cleaned = clean_name(unit.name)
        reference = Product(
            shop=unit.shop, name=cleaned, price=unit.new_price or unit.price,
            currency=unit.currency, url=unit.url, external_id=unit.code,
        )
        groups = await group_and_filter("", cleaned, "", [reference] + candidates)
        for group in groups:
            if any(p is reference for p in group.products):
                matched += [p for p in group.products if p is not reference and p not in matched]
                break

    return min(matched, key=lambda p: p.price) if matched else None


def _money(value: int, currency: str = "KZT") -> str:
    return f"{value:,}".replace(",", " ") + (" ₸" if currency == "KZT" else f" {currency}")


def _place(unit: DiscountedUnit) -> str:
    return ", ".join(x for x in (unit.address, unit.city) if x)


def _describe_unit(number: int, unit: DiscountedUnit, show_price: bool,
                    show_place: bool) -> list[str]:
    head = " · ".join(x for x in (
        _money(unit.price) if show_price else "",
        _place(unit) if show_place else "",
    ) if x)
    defects = [f"{tg.escape(title)}: {tg.escape(text)}"
               for title, text in unit.defects.items() if text]
    if head:
        return [f"<b>{number})</b> {tg.escape(head)}"] + [f"   • {d}" for d in defects]
    if not defects:
        return [f"<b>{number})</b> без описания"]
    # Ставить номер отдельной строкой не над чем — цены и адрес у всех общие,
    # поэтому нумеруем прямо первым дефектом.
    return [f"<b>{number})</b> {defects[0]}"] + [f"   • {d}" for d in defects[1:]]


SHOP_TITLES = {"sulpak": "Sulpak", "dns": "DNS", "kaspi": "Kaspi.kz",
               "technodom": "Технодом", "wildberries": "Wildberries"}


def shop_title(shop: str) -> str:
    return SHOP_TITLES.get(shop, shop)


def format_deal(units: list[DiscountedUnit], analog: Product | None) -> str:
    """Одно сообщение на модель. У одной модели бывает по 5-6 уценённых
    экземпляров в одном магазине (проверено: столько AirPods в Костанае) —
    отдельным сообщением на каждый бот превращается в спам, а сравнение с
    другими магазинами всё равно одно на всех."""
    first = units[0]
    prices = sorted({u.price for u in units})
    price_line = (_money(prices[0]) if len(prices) == 1
                  else f"от {_money(prices[0])} до {_money(prices[-1])}")

    discount = max(u.best_discount for u in units)
    lines = [f"🔥 <b>−{discount}%</b> · {tg.escape(first.name)}",
             f"<i>{shop_title(first.shop)} · уценка</i>", ""]
    lines.append(f"Цена: <b>{price_line}</b>")
    if first.new_price:
        vs_new = first.discount_vs_new
        lines.append(f"Новый там же: {_money(first.new_price)}"
                     + (f" (−{vs_new}%)" if vs_new else ""))
    if first.old_price:
        vs_old = first.discount_vs_old
        lines.append(f"Старая цена: {_money(first.old_price)}"
                     + (f" (−{vs_old}%)" if vs_old else ""))
    if first.rating:
        reviews = f", {first.reviews_count} отз." if first.reviews_count else ""
        lines.append(f"Оценка модели: {first.rating}{reviews}")

    places = {_place(u) for u in units if _place(u)}
    one_place = places.pop() if len(places) == 1 else ""
    if one_place:
        lines.append(f"📍 {tg.escape(one_place)}")

    shown = units[:MAX_UNITS_IN_MESSAGE]
    if len(units) == 1:
        lines += ["", "⚠️ <b>Что с ним не так:</b>"]
        lines += [f"• {tg.escape(title)}: {tg.escape(text)}"
                  for title, text in first.defects.items() if text]
    else:
        lines += ["", f"⚠️ <b>Экземпляров: {len(units)}</b>, у каждого свои дефекты:"]
        for number, unit in enumerate(shown, start=1):
            lines += _describe_unit(number, unit, show_price=len(prices) > 1,
                                     show_place=not one_place)
        if len(units) > len(shown):
            lines.append(f"…и ещё {len(units) - len(shown)} — смотри на странице товара")

    lines += ["", f'<a href="{first.url}">Открыть в {shop_title(first.shop)}</a>', ""]
    if analog:
        saving = analog.price - prices[0]
        lines.append(f"💰 Такой же новый дешевле всего: <b>{shop_title(analog.shop)} — "
                     f"{_money(analog.price, analog.currency)}</b>")
        lines.append(tg.escape(analog.name))
        if saving > 0:
            lines.append(f"Выгода против нового: <b>{_money(saving, analog.currency)}</b>")
        else:
            # Бывает: новый в другом магазине дешевле уценённого. Это ровно тот
            # случай, когда брать уценку не надо, и промолчать об этом нельзя.
            lines.append(f"⚠️ Новый дешевле уценённого на {_money(-saving, analog.currency)} — "
                         "уценка не выгодна.")
        lines.append(f'<a href="{analog.url}">Открыть предложение</a>')
    else:
        lines.append("💰 Такой же новый в других магазинах не нашёлся — сравнить не с чем.")
    return "\n".join(lines)


# Магазины, у которых фото — это снимки самого уценённого экземпляра. У Sulpak
# они лежат на отдельном CDN и показывают царапины; у DNS в карточке уценки
# обычная картинка модели из каталога, и назвать её «фото дефектов» было бы
# неправдой.
_UNIT_PHOTO_SHOPS = {"sulpak"}


def deal_buttons(units: list[DiscountedUnit]) -> dict:
    first = units[0]
    photos = sum(len(u.photos) for u in units)
    row = []
    if photos:
        what = "Фото дефектов" if first.shop in _UNIT_PHOTO_SHOPS else "Фото"
        row.append((f"📷 {what} ({photos})", f"ph:{first.document}"))
    row.append(("🔕 Скрыть модель", f"mute:{first.document}"))
    return tg.keyboard([row, [("🔎 Сравнить в магазинах", f"cmp:{first.document}")]])


_MIN_DISCOUNT_KEY = "deals_min_discount"
MIN_DISCOUNT_LIMITS = (1, 99)


def min_discount() -> int:
    """Порог скидки: заданный командой перевешивает значение из окружения."""
    saved = db.get_setting(_MIN_DISCOUNT_KEY)
    return int(saved) if saved and saved.isdigit() else DEALS_MIN_DISCOUNT


def set_min_discount(value: int) -> None:
    db.set_setting(_MIN_DISCOUNT_KEY, str(value))


def recipients() -> list[int]:
    chats = db.list_deals_subscribers()
    if not chats and DEALS_CHAT_ID:
        chats = [int(DEALS_CHAT_ID)]
    return chats


async def scan_all_shops() -> list[DiscountedUnit]:
    """Уценка всех магазинов, у которых она есть. Магазины обходятся параллельно:
    они независимы, и падение одного не должно лишать находок из другого."""
    adapters = registry.discounted_adapters()
    results = await asyncio.gather(
        *(a.scan_discounted() for a in adapters), return_exceptions=True
    )
    units: list[DiscountedUnit] = []
    for adapter, res in zip(adapters, results):
        if isinstance(res, Exception):
            log.warning("deals: обход уценки %s не удался: %r", adapter.name, res)
            continue
        log.info("deals: %s — уценённых экземпляров %s", adapter.name, len(res))
        units += res
    return units


async def _send_deal(chat_id: int, units: list[DiscountedUnit], analog: Product | None) -> bool:
    text = format_deal(units, analog)
    try:
        sent = await tg.send_message(chat_id, text, buttons=deal_buttons(units))
    except Exception:
        log.exception("deals: не отправилось в чат %s", chat_id)
        return False
    for unit in units:
        db.record_deal_message(unit.document, chat_id, sent.get("message_id", 0))
    return True


async def scan_once(force: bool = False, chats: list[int] | None = None) -> list[DiscountedUnit]:
    """Один обход уценки. force=True — не фильтровать уже показанные (ручная
    проверка «что там сейчас»), chats — кому отправлять вместо подписчиков."""
    chats = chats if chats is not None else recipients()
    if not chats:
        log.info("deals: некому отправлять находки, обход пропущен")
        return []

    async with _scan_lock:
        return await _scan(force, chats)


async def _scan(force: bool, chats: list[int]) -> list[DiscountedUnit]:
    threshold = min_discount()
    units = await scan_all_shops()
    log.info("deals: уценённых экземпляров всего %s", len(units))
    good = [u for u in units if u.best_discount >= threshold]
    good.sort(key=lambda u: -u.best_discount)
    log.info("deals: со скидкой >= %s%% — %s", threshold, len(good))
    if not good:
        return []

    if not force:
        fresh_docs = db.filter_new_deals([u.document for u in good])
        good = [u for u in good if u.document in fresh_docs]
        log.info("deals: из них ещё не показанных — %s", len(good))
    if not good:
        return []

    # Экземпляры одной модели — в одно сообщение: сравнение с другими магазинами
    # у них общее, а отдельным сообщением на каждый бот заваливает чат.
    by_model: dict[tuple[str, str], list[DiscountedUnit]] = {}
    for unit in good:
        by_model.setdefault((unit.shop, unit.code), []).append(unit)

    muted = {chat_id: db.muted_codes(chat_id) for chat_id in chats}
    models = [m for m in by_model.items() if any(m[0] not in muted[c] for c in chats)]
    # Порог может пропускать десяток моделей за раз — столько сообщений подряд
    # каждые три часа читать невозможно. Непоказанные не помечаются показанными
    # и придут следующим обходом, так что очередь не теряется.
    batch, rest = models[:DEALS_MAX_PER_SCAN], models[DEALS_MAX_PER_SCAN:]
    log.info("deals: моделей к отправке — %s (в очереди ещё %s)", len(batch), len(rest))

    sent_units: list[DiscountedUnit] = []
    for position, (key, model_units) in enumerate(batch):
        if position:
            # Пауза между поисками: Wildberries на серию запросов подряд начинает
            # отвечать 429 (ловилось и локально, и на сервере), и тогда
            # сравнивать становится не с чем.
            await asyncio.sleep(PAUSE_BETWEEN_LOOKUPS_SEC)
        try:
            analog = await find_cheapest_analog(model_units[0])
        except Exception:
            log.exception("deals: не смог сравнить с другими магазинами %s", model_units[0].name)
            analog = None

        delivered = False
        for chat_id in chats:
            if key in muted[chat_id]:
                continue
            delivered |= await _send_deal(chat_id, model_units, analog)
        # Помечаем показанными только реально доставленные находки: иначе сбой
        # отправки навсегда прячет товар, о котором никто не узнал.
        if delivered:
            sent_units += model_units
            for unit in model_units:
                db.mark_deal_notified(unit.document, unit.shop, unit.code, unit.name,
                                       unit.price, unit.best_discount, unit.url, unit.photos)

    if rest and sent_units:
        skipped = sum(len(u) for _, u in rest)
        top = ", ".join(f"−{max(u.best_discount for u in units)}%" for _, units in rest[:5])
        models_word = plural(len(rest), "модель", "модели", "моделей")
        units_word = plural(skipped, "экземпляр", "экземпляра", "экземпляров")
        for chat_id in chats:
            await tg.send_message(
                chat_id,
                f"…и ещё {len(rest)} {models_word} под порогом ({skipped} {units_word}): {top}\n"
                "Пришлю следующей проверкой или сразу по /deals scan.",
            )
    return sent_units


async def loop() -> None:
    while True:
        try:
            await scan_once()
        except Exception:
            log.exception("deals: обход уценки упал")
        await asyncio.sleep(DEALS_SCAN_INTERVAL_MIN * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    asyncio.run(scan_once())
