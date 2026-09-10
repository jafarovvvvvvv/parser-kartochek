import asyncio
import logging

import db
import deals
import tg
from adapters.dns import DnsAdapter
from adapters.kaspi import KaspiAdapter
from adapters.sulpak import SulpakAdapter
from adapters.technodom import TechnodomAdapter
from adapters.wildberries import WildberriesAdapter
from advice import recommend
from compare import MAX_GROUPS, format_comparison
from config import DEALS_MIN_DISCOUNT
from grouping import group_and_filter
from models import Product
from query_planner import plan as plan_query
from relevance import rerank
from voice import transcribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# Ozon пока не в строю — блокирует капчей на каждом запросе, "не отвечает"
# в ответе на каждый поиск был чистым шумом без пользы.
ADAPTERS = [
    WildberriesAdapter(),
    KaspiAdapter(),
    SulpakAdapter(),
    TechnodomAdapter(),
    DnsAdapter(),
]

DISPLAY_LIMIT = 4  # с запасом — одинаковые модели у разных магазинов схлопнёт группировка
# С запасом тянем больше кандидатов у магазина, чтобы было из чего выбирать
# после локальной фильтрации по релевантности — иначе нерелевантный товар
# из топ-3 магазина просто нечем заменить.
RAW_LIMIT = 15

# Результаты последнего поиска в чате, для команды /track N. Не переживает
# перезапуск процесса — это ок для MVP, история покупок хранится в БД.
_last_search: dict[int, list[Product]] = {}

# Незавершённый диалог уточнения: chat_id -> (накопленный текст, номер раунда).
# Тоже в памяти — переживать перезапуск не обязательно, просто попросим
# уточнить заново.
_pending_clarification: dict[int, tuple[str, int]] = {}
MAX_CLARIFY_ROUNDS = 2

HELP_TEXT = (
    "Пришли название товара текстом или голосом — сравню цены на Wildberries, "
    "Kaspi.kz, Sulpak, Технодоме и DNS. Если запрос слишком общий (например "
    "«хочу подарок»), сначала задам уточняющий вопрос.\n\n"
    "Команды:\n"
    "/track N — начать отслеживать цену N-го товара из последнего поиска\n"
    "/list — список отслеживаемых товаров\n"
    "/untrack ID — прекратить отслеживание\n"
    f"/deals on — присылать уценку Sulpak со скидкой от {DEALS_MIN_DISCOUNT}% "
    "(с дефектами экземпляра и ценой такого же нового в других магазинах)\n"
    "/deals off — перестать\n"
    "/deals scan — проверить уценку прямо сейчас"
)


async def search_all(search_query: str, relevance_query: str) -> tuple[dict[str, list[Product]], set[str]]:
    """search_query уходит в магазины (короткий, 1-3 слова — их поиск не
    понимает длинные предложения), relevance_query — полный текст пользователя,
    по нему же дожимаем ранжирование (чтобы не терять важные детали вроде цвета)."""
    results = await asyncio.gather(
        *(a.search(search_query, limit=RAW_LIMIT) for a in ADAPTERS), return_exceptions=True
    )
    out: dict[str, list[Product]] = {}
    errored: set[str] = set()
    for adapter, res in zip(ADAPTERS, results):
        if isinstance(res, Exception):
            log.warning("adapter %s failed: %r", adapter.name, res)
            out[adapter.name] = []
            errored.add(adapter.name)
        else:
            out[adapter.name] = rerank(relevance_query, res, DISPLAY_LIMIT)
    return out, errored


def _adapter_by_name(shop: str):
    return next(a for a in ADAPTERS if a.name == shop)


async def handle_text(chat_id: int, text: str) -> None:
    text = text.strip()
    if text in ("/start", "/help"):
        await tg.send_message(chat_id, HELP_TEXT)
        return

    if text.startswith("/track"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            await tg.send_message(chat_id, "Использование: /track N (N — номер из последнего поиска)")
            return
        idx = int(parts[1].strip()) - 1
        candidates = _last_search.get(chat_id, [])
        if not (0 <= idx < len(candidates)):
            await tg.send_message(chat_id, "Нет такого номера в последнем поиске. Сначала выполни поиск.")
            return
        p = candidates[idx]
        db.add_tracked_item(chat_id, p.shop, p.external_id, p.name, p.url, p.currency, p.price)
        await tg.send_message(chat_id, f"Отслеживаю: {p.name} ({p.price} {p.currency})")
        return

    if text == "/list":
        items = db.list_tracked_items_for_chat(chat_id)
        if not items:
            await tg.send_message(chat_id, "Пока ничего не отслеживается.")
            return
        lines = [f"#{i['id']} {i['name']} — {i['last_price']} {i['currency']}" for i in items]
        await tg.send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/untrack"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            await tg.send_message(chat_id, "Использование: /untrack ID")
            return
        ok = db.deactivate_tracked_item(int(parts[1].strip()), chat_id)
        await tg.send_message(chat_id, "Готово." if ok else "Не нашёл такой ID у тебя в списке.")
        return

    if text.startswith("/deals"):
        await handle_deals(chat_id, text)
        return

    if text.startswith("/"):
        await tg.send_message(chat_id, "Не знаю такую команду.\n\n" + HELP_TEXT)
        return

    pending = _pending_clarification.get(chat_id)
    context, rounds = pending if pending else ("", 0)
    full_text = f"{context}\n{text}" if context else text

    if rounds >= MAX_CLARIFY_ROUNDS:
        planned = {"action": "search", "query": full_text, "category": "", "criteria": ""}
    else:
        planned = await plan_query(text, context)

    if planned["action"] == "clarify":
        _pending_clarification[chat_id] = (full_text, rounds + 1)
        await tg.send_message(chat_id, planned["question"])
        return
    _pending_clarification.pop(chat_id, None)

    search_query = planned["query"]
    category = planned.get("category", "")
    criteria = planned.get("criteria", "")
    results, errored = await search_all(search_query, relevance_query=full_text)
    flat = [p for products in results.values() for p in products]

    if flat:
        await _enrich_specs(flat)
    groups = await group_and_filter(category, full_text, criteria, flat)

    # "Пусто" считаем ПОСЛЕ группировки, а не по сырой выдаче: магазин мог
    # что-то найти, но всё оказалось нерелевантным категории (ИИ отсеял) —
    # тогда магазин не должен просто молча пропадать из ответа.
    shops_with_offers = {p.shop for g in groups for p in g.products}
    empty_shops = [shop for shop in results if shop not in errored and shop not in shops_with_offers]

    text_out, numbered = format_comparison(full_text, groups, empty_shops, errored)
    _last_search[chat_id] = numbered
    await tg.send_message(chat_id, text_out)

    verdict = await recommend(full_text, criteria, groups[:MAX_GROUPS])
    if verdict:
        await tg.send_message(chat_id, verdict)


async def handle_deals(chat_id: int, text: str) -> None:
    arg = text[len("/deals"):].strip().lower()
    if arg == "on":
        db.add_deals_subscriber(chat_id)
        await tg.send_message(
            chat_id,
            f"Буду присылать уценку Sulpak со скидкой от {DEALS_MIN_DISCOUNT}% — "
            "с дефектами конкретного экземпляра и ценой такого же нового в других "
            "магазинах. Обход раздела уценки идёт по расписанию; чтобы посмотреть, "
            "что там есть прямо сейчас — /deals scan.",
        )
        return
    if arg == "off":
        removed = db.remove_deals_subscriber(chat_id)
        await tg.send_message(chat_id, "Больше не присылаю." if removed else "Ты и так не подписан.")
        return
    if arg == "scan":
        await tg.send_message(chat_id, "Смотрю уценку Sulpak, это займёт пару минут…")
        # force=True — показать текущие находки целиком, даже если о них уже
        # сообщали: это ручная проверка «что там сейчас», а не алерт.
        found = await deals.scan_once(force=True, chats=[chat_id])
        if not found:
            await tg.send_message(
                chat_id, f"Сейчас нет уценки со скидкой от {DEALS_MIN_DISCOUNT}%."
            )
        return

    subscribed = chat_id in db.list_deals_subscribers()
    await tg.send_message(
        chat_id,
        ("Уценка Sulpak: подписка включена." if subscribed else "Уценка Sulpak: подписки нет.")
        + f"\nПорог скидки — от {DEALS_MIN_DISCOUNT}%."
        + "\n\n/deals on — включить\n/deals off — выключить\n/deals scan — проверить сейчас",
    )


async def handle_voice(chat_id: int, file_id: str) -> None:
    try:
        text = await transcribe(file_id)
    except Exception as exc:
        log.exception("не смог распознать голосовое от %s", chat_id)
        await tg.send_message(chat_id, f"⚠️ не смог распознать голосовое: {exc}")
        return
    if not text:
        await tg.send_message(chat_id, "🤷 пустая расшифровка, скажи ещё раз")
        return
    await tg.send_message(chat_id, f"🎙 понял: {text}")
    await handle_text(chat_id, text)


async def _enrich_specs(products: list[Product]) -> None:
    """Подтягивает полную таблицу характеристик со страницы товара там, где
    это возможно (сейчас — только Kaspi), чтобы ИИ сравнивал не только
    по названию."""
    kaspi = _adapter_by_name("kaspi")
    kaspi_products = [p for p in products if p.shop == "kaspi" and p.external_id]

    async def fill(p: Product) -> None:
        try:
            p.specs.update(await kaspi.fetch_specs(p.external_id))
        except Exception:
            log.warning("не удалось получить характеристики %s", p.url)

    if kaspi_products:
        await asyncio.gather(*(fill(p) for p in kaspi_products))


async def poll_loop() -> None:
    offset = int(db.get_setting("tg_offset", "0"))
    log.info("bot started, offset=%s", offset)
    while True:
        try:
            updates = await tg.get_updates(offset=offset or None)
        except Exception:
            log.exception("get_updates failed, retrying in 5s")
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            # Сохраняем офсет ДО обработки, а не после: обработка (поиск по
            # нескольким магазинам + ИИ) может идти дольше минуты, и если
            # процесс перезапустят (например, /deploy) в этот момент, Telegram
            # пришлёт то же сообщение повторно — лучше потерять один апдейт
            # при падении, чем дублировать ответ пользователю при каждом рестарте.
            db.set_setting("tg_offset", str(offset))
            message = update.get("message")
            if not message:
                continue
            chat_id = message["chat"]["id"]
            voice = message.get("voice") or message.get("audio")
            try:
                if voice:
                    await handle_voice(chat_id, voice["file_id"])
                elif "text" in message:
                    await handle_text(chat_id, message["text"])
            except Exception:
                log.exception("failed to handle message from %s", chat_id)
                await tg.send_message(chat_id, "Что-то сломалось, попробуй ещё раз.")


async def main() -> None:
    db.init_db()
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
