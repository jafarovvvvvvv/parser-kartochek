import html

from adapters.base import ShopAdapter
from config import TECHNODOM_CITY_ID, TECHNODOM_CITY_SLUG
from http_client import post_json
from models import Product

SEARCH_URL = "https://api.technodom.kz/katalog/api/v2/products/search"
PRODUCT_URL = "https://www.technodom.kz/" + TECHNODOM_CITY_SLUG + "/p/{uri}"


def _base_payload(query: str) -> dict:
    return {
        "categories": [""],
        "city_id": TECHNODOM_CITY_ID,
        "client_id": "kartochek",
        "filters": {},
        "price_range": {"max": 0, "min": 0},
        "query": query,
        "referer": "https://www.technodom.kz/",
        "session_id": "kartochek",
        "sort_by": "popular",
        "sort_order": "",
        "type": "full_search",
    }


def _specs_from_short_description(item: dict) -> dict[str, str]:
    specs = {}
    for entry in item.get("short_description") or []:
        title = entry.get("title")
        values = [v.get("value_ru") for v in entry.get("values") or [] if v.get("value_ru")]
        if title and values:
            specs[html.unescape(title)] = html.unescape(", ".join(values))
    return specs


def _to_product(item: dict) -> Product | None:
    try:
        price = int(float(item["price"]))
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0:
        return None
    old_price = None
    try:
        op = int(float(item.get("old_price") or 0))
        old_price = op if op > price else None
    except (TypeError, ValueError):
        pass
    return Product(
        shop="technodom",
        name=html.unescape(item.get("title", "")),
        price=price,
        currency="KZT",
        url=PRODUCT_URL.format(uri=item["uri"]),
        old_price=old_price,
        rating=(item["rating"] / 20) if item.get("rating") else None,  # 0-100 -> 0-5
        reviews_count=item.get("reviews"),
        specs=_specs_from_short_description(item),
        external_id=item.get("sku"),
    )


class TechnodomAdapter(ShopAdapter):
    name = "technodom"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        payload = _base_payload(query)
        payload.update({"limit": limit, "page": 1})
        data = await post_json(SEARCH_URL, json_body=payload)
        items = data.get("products", [])
        return [p for p in (_to_product(i) for i in items) if p]

    async def fetch_price(self, external_id: str) -> Product | None:
        payload = _base_payload(external_id)
        payload.update({"limit": 5, "page": 1})
        data = await post_json(SEARCH_URL, json_body=payload)
        for item in data.get("products", []):
            if item.get("sku") == external_id:
                return _to_product(item)
        return None
