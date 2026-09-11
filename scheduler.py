import asyncio
import logging

import db
import tg
from adapters import registry
from config import PRICE_CHECK_INTERVAL_MIN

log = logging.getLogger("scheduler")


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def price_drop_text(item, new_price: int) -> str:
    old = item["last_price"]
    drop = old - new_price
    percent = round(drop / old * 100) if old else 0
    # Всё, что пришло из магазина (название, ссылка), — через escape: разметка
    # HTML, и сырые < > & в названии Telegram не примет.
    return (
        f"📉 <b>Подешевело на {percent}%</b>\n"
        f'<a href="{tg.escape(item["url"])}">{tg.escape(item["name"])}</a>\n'
        f"{_money(old)} → <b>{_money(new_price)} {item['currency']}</b> (−{_money(drop)})"
    )


async def check_once() -> None:
    # Один и тот же товар нередко отслеживают из нескольких чатов — группируем
    # по (магазин, id товара), чтобы сходить в магазин один раз за круг, а не
    # по разу на каждого подписчика, и разослать результат всем сразу.
    groups: dict[tuple[str, str], list] = {}
    for item in db.list_active_tracked_items():
        groups.setdefault((item["shop"], item["external_id"]), []).append(item)

    for (shop, external_id), subscribers in groups.items():
        adapter = registry.by_name(shop)
        if adapter is None:
            continue
        try:
            fresh = await adapter.fetch_price(external_id)
        except Exception:
            log.exception("fetch_price failed for %s/%s", shop, external_id)
            continue
        if fresh is None:
            continue
        for item in subscribers:
            db.record_price(item["id"], fresh.price)
            if fresh.price < item["last_price"]:
                try:
                    await tg.send_message(
                        item["chat_id"],
                        price_drop_text(item, fresh.price),
                        buttons=tg.keyboard([[("❌ Перестать следить", f"untrack:{item['id']}")]]),
                    )
                except Exception:
                    # Сбой отправки одному подписчику не должен обрывать рассылку
                    # остальным и проверку следующих товаров в этом же круге.
                    log.exception("send_message failed for chat %s", item["chat_id"])


async def loop() -> None:
    while True:
        try:
            await check_once()
        except Exception:
            log.exception("price check round failed")
        await asyncio.sleep(PRICE_CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    asyncio.run(loop())
