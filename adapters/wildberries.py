import logging

from adapters.base import ShopAdapter
from config import WB_DEST
from http_client import get_json
from models import Product

log = logging.getLogger(__name__)

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v9/search"
DETAIL_URL = "https://card.wb.ru/cards/v4/detail"

# Формула basket-хоста для картинок по vol/part непубличная и часто меняется
# (проверенные диапазоны 2024 года уже не бьют с текущим CDN) — картинку не
# строим, чтобы не отдавать в бот заведомо битые ссылки.


def _to_product(item: dict) -> Product:
    nm_id = item["id"]
    size = item["sizes"][0]
    price_kzt = size["price"]["product"] // 100
    return Product(
        shop="wildberries",
        name=item["name"],
        price=price_kzt,
        currency="KZT",
        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        image=None,
        rating=item.get("reviewRating") or item.get("rating"),
        reviews_count=item.get("feedbacks"),
        specs={"brand": item.get("brand", "")},
        external_id=str(nm_id),
    )


class WildberriesAdapter(ShopAdapter):
    name = "wildberries"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        data = await get_json(
            SEARCH_URL,
            params={
                "ab_testid": "false",
                "appType": 1,
                "curr": "kzt",
                "dest": WB_DEST,
                "query": query,
                "resultset": "catalog",
                "spp": 30,
            },
        )
        items = data.get("products", [])[:limit]
        products = [_to_product(item) for item in items if item.get("sizes")]
        log.info("wb search %r: %d сырых, %d с ценой", query, len(data.get("products", [])), len(products))
        return products

    async def fetch_price(self, external_id: str) -> Product | None:
        data = await get_json(
            DETAIL_URL,
            params={
                "ab_testid": "false",
                "appType": 1,
                "curr": "kzt",
                "dest": WB_DEST,
                "spp": 30,
                "nm": external_id,
            },
        )
        items = data.get("products", [])
        if not items or not items[0].get("sizes"):
            return None
        return _to_product(items[0])
