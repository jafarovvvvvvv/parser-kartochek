import asyncio
import logging

import db
import tg
from adapters.dns import DnsAdapter
from adapters.kaspi import KaspiAdapter
from adapters.ozon import OzonAdapter
from adapters.sulpak import SulpakAdapter
from adapters.technodom import TechnodomAdapter
from adapters.wildberries import WildberriesAdapter
from config import PRICE_CHECK_INTERVAL_MIN

log = logging.getLogger("scheduler")

ADAPTERS = {
    a.name: a
    for a in (
        WildberriesAdapter(),
        OzonAdapter(),
        KaspiAdapter(),
        SulpakAdapter(),
        TechnodomAdapter(),
        DnsAdapter(),
    )
}


async def check_once() -> None:
    for item in db.list_active_tracked_items():
        adapter = ADAPTERS.get(item["shop"])
        if adapter is None:
            continue
        try:
            fresh = await adapter.fetch_price(item["external_id"])
        except Exception:
            log.exception("fetch_price failed for %s/%s", item["shop"], item["external_id"])
            continue
        if fresh is None:
            continue
        db.record_price(item["id"], fresh.price)
        if fresh.price < item["last_price"]:
            drop = item["last_price"] - fresh.price
            await tg.send_message(
                item["chat_id"],
                f"📉 Цена снизилась: {item['name']}\n"
                f"{item['last_price']} → {fresh.price} {item['currency']} (−{drop})\n"
                f"{item['url']}",
            )


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
