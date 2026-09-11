import asyncio
import logging

import db
import deals
import tg
from adapters import registry
from advice import recommend
from compare import MAX_GROUPS, format_comparison, track_buttons
from grouping import group_and_filter
from models import Product
from query_planner import plan as plan_query
from relevance import rerank
from voice import transcribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

ADAPTERS = registry.SEARCH

DISPLAY_LIMIT = 4  # с запасом — одинаковые модели у разных магазинов схлопнёт группировка
# С запасом тянем больше кандидатов у магазина, чтобы было из чего выбирать
# после локальной фильтрации по релевантности — иначе нерелевантный товар
# из топ-3 магазина просто нечем заменить.
RAW_LIMIT = 15

# Результаты последнего поиска в чате, для кнопок «отслеживать». Не переживают
# перезапуск процесса — это ок, кнопка тогда попросит поискать заново.
_last_search: dict[int, list[Product]] = {}

# Незавершённый диалог уточнения: chat_id -> (накопленный текст, номер раунда).
_pending_clarification: dict[int, tuple[str, int]] = {}
MAX_CLARIFY_ROUNDS = 2

# Список для меню команд Telegram: команды подсказываются прямо в интерфейсе,
# и их не нужно помнить или искать в /help.
COMMANDS = [
    ("deals", "Уценка: находки со скидкой"),
    ("settings", "Настройки и порог скидки"),
    ("list", "Отслеживаемые товары"),
    ("cancel", "Отменить уточнение"),
    ("help", "Что умеет бот"),
]

HELP_TEXT = (
    "Пришли название товара текстом или голосом — сравню цены на Wildberries, "
    "Kaspi.kz, Sulpak, Технодоме и DNS. Под карточками будут кнопки 📌, чтобы "
    "поставить товар на отслеживание цены.\n\n"
    "<b>Уценка.</b> Бот сам обходит разделы уценённых товаров Sulpak и DNS и "
    "присылает находки: скидка, дефекты конкретного экземпляра, фото этих "
    "дефектов и цена такого же нового в других магазинах.\n\n"
    "<b>Команды</b>\n"
    "/deals — уценка: подписка, проверить сейчас\n"
    "/settings — порог скидки и что сейчас настроено\n"
    "/list — отслеживаемые товары\n"
    "/cancel — сбросить незаконченное уточнение"
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


async def handle_text(chat_id: int, text: str) -> None:
    text = text.strip()
    if text in ("/start", "/help"):
        await tg.send_message(chat_id, HELP_TEXT)
        return

    if text == "/cancel":
        had = _pending_clarification.pop(chat_id, None)
        await tg.send_message(chat_id, "Сбросил." if had else "Нечего отменять.")
        return

    if text.startswith("/track"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            await tg.send_message(chat_id, "Использование: /track N (или нажми 📌 под карточкой)")
            return
        await track_by_number(chat_id, int(parts[1].strip()))
        return

    if text == "/list":
        await show_tracked(chat_id)
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

    if text.startswith("/settings"):
        await show_settings(chat_id)
        return

    if text.startswith("/"):
        await tg.send_message(chat_id, "Не знаю такую команду.\n\n" + HELP_TEXT)
        return

    await run_search(chat_id, text)


async def run_search(chat_id: int, text: str) -> None:
    pending = _pending_clarification.get(chat_id)
    context, rounds = pending if pending else ("", 0)
    full_text = f"{context}\n{text}" if context else text

    if rounds >= MAX_CLARIFY_ROUNDS:
        planned = {"action": "search", "query": full_text, "category": "", "criteria": ""}
    else:
        planned = await plan_query(text, context)

    if planned["action"] == "clarify":
        _pending_clarification[chat_id] = (full_text, rounds + 1)
        await tg.send_message(chat_id, tg.escape(planned["question"])
                              + "\n\n<i>Не то? /cancel — начать заново.</i>")
        return
    _pending_clarification.pop(chat_id, None)

    await tg.send_chat_action(chat_id)
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
    await tg.send_message(chat_id, text_out, buttons=track_buttons(numbered))

    verdict = await recommend(full_text, criteria, groups[:MAX_GROUPS])
    if verdict:
        await tg.send_message(chat_id, tg.escape(verdict))


async def track_by_number(chat_id: int, number: int) -> str:
    candidates = _last_search.get(chat_id, [])
    idx = number - 1
    if not (0 <= idx < len(candidates)):
        message = "Не нашёл этот номер — поищи заново, результаты прошлого поиска уже не в памяти."
        await tg.send_message(chat_id, message)
        return message
    p = candidates[idx]
    db.add_tracked_item(chat_id, p.shop, p.external_id, p.name, p.url, p.currency, p.price)
    message = f"Отслеживаю: {p.name} ({p.price} {p.currency}). Сообщу, когда подешевеет."
    await tg.send_message(chat_id, tg.escape(message))
    return "Поставил на отслеживание"


async def show_tracked(chat_id: int) -> None:
    items = db.list_tracked_items_for_chat(chat_id)
    if not items:
        await tg.send_message(chat_id, "Пока ничего не отслеживается. Найди товар и нажми 📌.")
        return
    lines, rows = [], []
    for i in items:
        price = f"{i['last_price']:,}".replace(",", " ")
        lines.append(f'• <a href="{i["url"]}">{tg.escape(i["name"])}</a> — <b>{price} {i["currency"]}</b>')
        rows.append([(f"❌ {i['name'][:28]}", f"untrack:{i['id']}")])
    await tg.send_message(chat_id, "<b>Отслеживаю:</b>\n" + "\n".join(lines),
                          buttons=tg.keyboard(rows))


def settings_text() -> str:
    return (
        "<b>Настройки</b>\n\n"
        f"Порог скидки: <b>от {deals.min_discount()}%</b>\n"
        f"Магазины с уценкой: {', '.join(deals.shop_title(s) for s in registry.DISCOUNTED)}\n"
        f"Проверка: каждые {deals.DEALS_SCAN_INTERVAL_MIN // 60} ч, "
        f"не больше {deals.DEALS_MAX_PER_SCAN} находок за раз"
    )


def settings_buttons(chat_id: int) -> dict:
    subscribed = chat_id in db.list_deals_subscribers()
    muted = len(db.list_muted(chat_id))
    return tg.keyboard([
        [("−5%", "set:disc:-5"), ("порог", "noop"), ("+5%", "set:disc:+5")],
        [("🔕 Отписаться", "deals:off") if subscribed else ("🔔 Подписаться", "deals:on")],
        [("🔎 Проверить сейчас", "deals:scan")],
        [(f"🙈 Скрытые модели: {muted}", "muted:list")] if muted else [("🔄 Показать находки заново", "deals:again")],
    ])


async def show_settings(chat_id: int) -> None:
    await tg.send_message(chat_id, settings_text(), buttons=settings_buttons(chat_id))


async def _run_manual_scan(chat_id: int, force: bool = False) -> None:
    try:
        found = await deals.scan_once(force=force, chats=[chat_id])
    except Exception:
        log.exception("ручной обход уценки упал")
        await tg.send_message(chat_id, "Не смог посмотреть уценку, попробуй позже.")
        return
    if not found:
        await tg.send_message(
            chat_id,
            f"Новых находок со скидкой от {deals.min_discount()}% нет.\n"
            "Снизить порог — /settings, показать уже виденные заново — кнопка «Показать находки заново».",
        )


async def handle_deals(chat_id: int, text: str) -> None:
    arg = text[len("/deals"):].strip().lower()
    if arg == "on":
        db.add_deals_subscriber(chat_id)
        await tg.send_message(
            chat_id,
            f"Буду присылать уценку со скидкой от {deals.min_discount()}% — с дефектами "
            "конкретного экземпляра, фото и ценой такого же нового в других магазинах.",
            buttons=tg.keyboard([[("🔎 Проверить сейчас", "deals:scan")],
                                 [("⚙️ Настройки", "settings")]]),
        )
        return
    if arg == "off":
        removed = db.remove_deals_subscriber(chat_id)
        await tg.send_message(chat_id, "Больше не присылаю." if removed else "Ты и так не подписан.")
        return

    threshold_arg = arg.removeprefix("порог").strip().rstrip("%")
    if threshold_arg.isdigit():
        await set_threshold(chat_id, int(threshold_arg))
        return

    if arg in ("scan", "again"):
        await tg.send_message(chat_id, "Смотрю уценку, это займёт несколько минут…")
        if arg == "again":
            db.forget_deals()
        asyncio.create_task(_run_manual_scan(chat_id))
        return

    await show_settings(chat_id)


async def set_threshold(chat_id: int, value: int) -> str:
    low, high = deals.MIN_DISCOUNT_LIMITS
    if not low <= value <= high:
        await tg.send_message(chat_id, f"Порог должен быть от {low} до {high}%.")
        return "Порог вне допустимого"
    deals.set_min_discount(value)
    await tg.send_message(
        chat_id,
        f"Порог скидки: <b>от {value}%</b>. Проверить, что под него подходит сейчас — /deals scan",
    )
    return f"Порог: от {value}%"


async def handle_callback(query: dict) -> None:
    data = query.get("data") or ""
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    action, _, argument = data.partition(":")
    notice = "Готово"

    try:
        if action == "track":
            notice = await track_by_number(chat_id, int(argument))
        elif action == "untrack":
            ok = db.deactivate_tracked_item(int(argument), chat_id)
            notice = "Снял с отслеживания" if ok else "Уже снято"
            await show_tracked(chat_id)
        elif action == "ph":
            notice = await send_deal_photos(chat_id, argument)
        elif action == "mute":
            notice = await mute_deal(chat_id, message_id, argument)
        elif action == "cmp":
            deal = db.get_deal(argument)
            if not deal:
                notice = "Не помню эту находку"
            else:
                notice = "Ищу в других магазинах"
                asyncio.create_task(run_search(chat_id, deals.clean_name(deal["name"])))
        elif action == "deals":
            await handle_deals(chat_id, f"/deals {argument}")
            notice = "Ок"
        elif action == "settings":
            await show_settings(chat_id)
        elif action == "muted":
            await show_muted(chat_id)
        elif action == "unmute":
            shop, _, code = argument.partition(":")
            db.unmute_model(chat_id, shop, code)
            notice = "Снова буду показывать"
            await show_muted(chat_id)
        elif action == "set":
            kind, _, value = argument.partition(":")
            if kind == "disc":
                notice = await set_threshold(chat_id, deals.min_discount() + int(value))
                await tg.edit_message_text(chat_id, message_id, settings_text(),
                                           buttons=settings_buttons(chat_id))
        elif action == "noop":
            notice = f"Сейчас от {deals.min_discount()}%"
    except Exception:
        log.exception("не смог обработать нажатие %r", data)
        notice = "Что-то сломалось"

    # Без ответа кнопка у пользователя крутит «часики» до таймаута клиента.
    await tg.answer_callback(query["id"], notice)


async def send_deal_photos(chat_id: int, document: str) -> str:
    photos = db.deal_photos(document)
    if not photos:
        return "Фото не сохранились"
    try:
        await tg.send_photo_group(chat_id, photos[:10])
    except Exception:
        log.exception("не смог отправить фото находки %s", document)
        # Telegram скачивает картинку сам и иногда не может — тогда пусть у
        # пользователя останутся хотя бы ссылки, это лучше пустого отказа.
        await tg.send_message(chat_id, "Фото не загрузились, вот ссылки:\n" + "\n".join(photos[:5]),
                              disable_preview=False)
        return "Отправил ссылками"
    return f"Фото: {len(photos[:10])}"


async def mute_deal(chat_id: int, message_id: int, document: str) -> str:
    deal = db.get_deal(document)
    if not deal:
        return "Не помню эту находку"
    db.mute_model(chat_id, deal["shop"], deal["code"], deal["name"])
    await tg.edit_message_buttons(chat_id, message_id, tg.keyboard(
        [[("🔔 Вернуть модель", f"unmute:{deal['shop']}:{deal['code']}")]]
    ))
    return "Больше не покажу эту модель"


async def show_muted(chat_id: int) -> None:
    rows = db.list_muted(chat_id)
    if not rows:
        await tg.send_message(chat_id, "Скрытых моделей нет.")
        return
    buttons = [[(f"🔔 {r['name'][:30]}", f"unmute:{r['shop']}:{r['code']}")] for r in rows[:10]]
    await tg.send_message(chat_id, "<b>Скрытые модели</b>\nНажми, чтобы снова показывать:",
                          buttons=tg.keyboard(buttons))


async def handle_voice(chat_id: int, file_id: str) -> None:
    try:
        text = await transcribe(file_id)
    except Exception as exc:
        log.exception("не смог распознать голосовое от %s", chat_id)
        await tg.send_message(chat_id, f"⚠️ не смог распознать голосовое: {tg.escape(str(exc))}")
        return
    if not text:
        await tg.send_message(chat_id, "🤷 пустая расшифровка, скажи ещё раз")
        return
    await tg.send_message(chat_id, f"🎙 понял: <b>{tg.escape(text)}</b>\n"
                                   "<i>Если не то — пришли текстом.</i>")
    await handle_text(chat_id, text)


async def _enrich_specs(products: list[Product]) -> None:
    """Подтягивает полную таблицу характеристик со страницы товара там, где
    это возможно (сейчас — только Kaspi), чтобы ИИ сравнивал не только
    по названию."""
    kaspi = registry.by_name("kaspi")
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
    try:
        # Меню команд регистрируем здесь, а не в main(): точка входа проекта
        # (main.py) зовёт именно poll_loop, а не bot.main().
        await tg.set_my_commands(COMMANDS)
    except Exception:
        log.exception("не смог зарегистрировать меню команд")
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
            try:
                db.set_setting("tg_offset", str(offset))
            except Exception:
                log.exception("не смог сохранить офсет, продолжаю")

            try:
                if "callback_query" in update:
                    await handle_callback(update["callback_query"])
                    continue
                message = update.get("message")
                if not message:
                    continue
                chat_id = message["chat"]["id"]
                voice = message.get("voice") or message.get("audio")
                if voice:
                    await handle_voice(chat_id, voice["file_id"])
                elif "text" in message:
                    await handle_text(chat_id, message["text"])
            except Exception:
                log.exception("failed to handle update %s", update.get("update_id"))
                try:
                    await tg.send_message(update.get("message", {}).get("chat", {}).get("id")
                                          or update["callback_query"]["message"]["chat"]["id"],
                                          "Что-то сломалось, попробуй ещё раз.")
                except Exception:
                    log.exception("не смог сообщить об ошибке пользователю")


async def main() -> None:
    db.init_db()
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
