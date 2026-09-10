"""Охота за уценкой Sulpak: периодически обходит раздел уценённых товаров,
ловит экземпляры со скидкой выше порога и шлёт находку в Telegram — с дефектами
конкретного экземпляра и ценой такого же нового товара в других магазинах.

В отличие от scheduler.py (следит за ценой того, что пользователь сам выбрал),
здесь триггер — расписание и порог выгодности, а не запрос пользователя."""
import asyncio
import logging
import re

import db
import tg
from adapters.dns import DnsAdapter
from adapters.kaspi import KaspiAdapter
from adapters.sulpak import SulpakAdapter
from adapters.technodom import TechnodomAdapter
from adapters.wildberries import WildberriesAdapter
from config import DEALS_CHAT_ID, DEALS_MIN_DISCOUNT, DEALS_SCAN_INTERVAL_MIN, OPENAI_API_KEY
from grouping import group_and_filter
from models import DiscountedUnit, Product
from relevance import rerank

log = logging.getLogger("deals")

SULPAK = SulpakAdapter()
# Магазины для поиска такого же нового товара. Сам Sulpak не нужен — с ним и
# сравниваем.
RIVALS = [WildberriesAdapter(), KaspiAdapter(), TechnodomAdapter(), DnsAdapter()]

RAW_LIMIT = 12
PER_SHOP = 3
PAUSE_BETWEEN_LOOKUPS_SEC = 4
# Названия в Sulpak длинные и с повторами ("FreeBuds 5i T0014 Isle Blue FreeBuds
# 5i Isle Blue"), а поиск магазинов на длинных строках отдаёт пустоту — см.
# README про query_planner. Поэтому в чужие магазины уходит короткий запрос.
QUERY_WORDS = 5


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
    """Название без повторов. Sulpak дублирует модель и цвет
    ("FreeBuds 5i T0014 Isle Blue FreeBuds 5i Isle Blue") — на таком названии
    ИИ хуже узнаёт товар в чужой выдаче и не собирает группу."""
    return " ".join(_unique_words(name))


def short_query(name: str) -> str:
    return " ".join(_unique_words(re.sub(r"\([^)]*\)", " ", name))[:QUERY_WORDS])


_MODEL_CODE_RE = re.compile(r"[a-z0-9][a-z0-9\-/]{3,}", re.I)


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


async def find_cheapest_analog(unit: DiscountedUnit) -> Product | None:
    """Самый дешёвый ТОЧНО такой же новый товар в других магазинах.

    Два независимых способа опознать «тот же самый товар», результаты
    объединяются: строгое совпадение по артикулу модели (работает без ИИ и почти
    не ошибается) и та же группировка ИИ, что и в обычном поиске — подкладываем
    уценённый товар в список кандидатов и берём то, что ИИ положил с ним в одну
    группу. Порознь каждый способ неполон: у Kaspi в названии нет артикула, а
    без ключа ИИ недоступен вовсе. Что не опознано — не предлагаем: лучше
    промолчать, чем прислать «аналог», который на деле другая модель."""
    query = short_query(unit.name)
    results = await asyncio.gather(
        *(a.search(query, limit=RAW_LIMIT) for a in RIVALS), return_exceptions=True
    )
    candidates: list[Product] = []
    for adapter, res in zip(RIVALS, results):
        if isinstance(res, Exception):
            log.warning("deals: магазин %s не ответил на %r: %r", adapter.name, query, res)
            continue
        candidates += rerank(unit.name, res, PER_SHOP)
    if not candidates:
        return None

    matched = [p for p in candidates if same_model(unit.name, p.name)]

    # Артикул есть не везде: Kaspi пишет "Наушники Huawei FreeBuds 5i чёрный"
    # без кода модели, и такой товар строгое сравнение пропустит. Поэтому,
    # когда ИИ доступен, добираем совпадения ещё и им.
    if OPENAI_API_KEY:
        cleaned = clean_name(unit.name)
        reference = Product(
            shop="sulpak", name=cleaned, price=unit.new_price or unit.price,
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


def format_deal(unit: DiscountedUnit, analog: Product | None) -> str:
    lines = [f"🔥 Уценка −{unit.best_discount}% · {unit.name}", ""]
    lines.append(f"Цена уценённого: {_money(unit.price)}")
    if unit.new_price:
        vs_new = unit.discount_vs_new
        lines.append(f"Новый в Sulpak: {_money(unit.new_price)}"
                     + (f" (−{vs_new}%)" if vs_new else ""))
    if unit.old_price:
        vs_old = unit.discount_vs_old
        lines.append(f"Старая цена: {_money(unit.old_price)}"
                     + (f" (−{vs_old}%)" if vs_old else ""))

    if unit.defects:
        lines += ["", "⚠️ Что с ним не так:"]
        lines += [f"• {title}: {text}" for title, text in unit.defects.items() if text]

    where = ", ".join(x for x in (unit.address, unit.city) if x)
    if where:
        lines += ["", f"📍 {where}"]
    lines.append(unit.url)

    lines.append("")
    if analog:
        saving = analog.price - unit.price
        lines.append(f"💰 Такой же новый дешевле всего: {analog.shop} — "
                     f"{_money(analog.price, analog.currency)}")
        lines.append(analog.name)
        if saving > 0:
            lines.append(f"Выгода против нового: {_money(saving, analog.currency)}")
        else:
            # Бывает: новый в другом магазине дешевле уценённого. Это ровно тот
            # случай, когда брать уценку не надо, и промолчать об этом нельзя.
            lines.append(f"⚠️ Новый дешевле уценённого на {_money(-saving, analog.currency)} — "
                         "уценка не выгодна.")
        lines.append(analog.url)
    else:
        lines.append("💰 Такой же новый в других магазинах не нашёлся — сравнить не с чем.")
    return "\n".join(lines)


def recipients() -> list[int]:
    chats = db.list_deals_subscribers()
    if not chats and DEALS_CHAT_ID:
        chats = [int(DEALS_CHAT_ID)]
    return chats


async def scan_once(force: bool = False, chats: list[int] | None = None) -> list[DiscountedUnit]:
    """Один обход уценки. force=True — не фильтровать уже показанные (ручная
    проверка «что там сейчас»), chats — кому отправлять вместо подписчиков."""
    chats = chats if chats is not None else recipients()
    if not chats:
        log.info("deals: некому отправлять находки, обход пропущен")
        return []

    units = await SULPAK.scan_discounted()
    log.info("deals: уценённых экземпляров всего %s", len(units))
    good = [u for u in units if u.best_discount >= DEALS_MIN_DISCOUNT]
    good.sort(key=lambda u: -u.best_discount)
    log.info("deals: со скидкой >= %s%% — %s", DEALS_MIN_DISCOUNT, len(good))
    if not good:
        return []

    if not force:
        fresh_docs = db.filter_new_deals([u.document for u in good])
        good = [u for u in good if u.document in fresh_docs]
        log.info("deals: из них ещё не показанных — %s", len(good))
    if not good:
        return []

    # У одной модели бывает несколько уценённых экземпляров с разными дефектами —
    # искать по чужим магазинам один и тот же товар повторно незачем.
    analogs: dict[str, Product | None] = {}
    for unit in good:
        if unit.code not in analogs:
            if analogs:
                # Пауза между поисками: Wildberries на серию запросов подряд
                # начинает отвечать 429 (ловилось и локально, и на сервере), и
                # тогда сравнивать становится не с чем.
                await asyncio.sleep(PAUSE_BETWEEN_LOOKUPS_SEC)
            try:
                analogs[unit.code] = await find_cheapest_analog(unit)
            except Exception:
                log.exception("deals: не смог сравнить с другими магазинами %s", unit.name)
                analogs[unit.code] = None
        analog = analogs[unit.code]
        text = format_deal(unit, analog)
        for chat_id in chats:
            try:
                # Без разметки: в описании дефектов от Sulpak попадаются * _ [ ],
                # и как Markdown такое сообщение Telegram не примет. Форматировать
                # тут нечего, поэтому шлём сразу текстом, а не через отказ и повтор.
                await tg.send_message(chat_id, text, parse_mode=None)
            except Exception:
                log.exception("deals: не отправилось в чат %s", chat_id)
        db.mark_deal_notified(unit.document, unit.code, unit.name, unit.price, unit.best_discount)
    return good


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
