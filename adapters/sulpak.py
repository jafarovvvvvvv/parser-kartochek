import html
import re
from urllib.parse import quote

from adapters.base import ShopAdapter
from http_client import get_text, post_json
from models import Product

SEARCH_URL = "https://www.sulpak.kz/Search/LoadProducts/1/77/~/~/~/~/~/PopularityDesc/tiles"
PRODUCT_URL = "https://www.sulpak.kz{path}"

_SEARCH_BODY = "page=1&classes=77&price=&propertiesTokens=&stocks=&listing=tiles&discountRangesToken="

_ITEM_RE = re.compile(
    r'<div class="product__item product__item-js tile-container"\s+'
    r'data-name="(?P<name>[^"]+)"\s+data-code="(?P<code>[^"]+)"\s+'
    r'data-price="(?P<price>[^"]+)"\s+data-brand="(?P<brand>[^"]*)"',
)
_HREF_RE = re.compile(r'href="(?P<href>/g/[^"]+)"')

# Sulpak ставит ровно эту цену товарам "Нет в наличии" вместо реальной —
# не настоящая цена, отсекаем (проверено на живом товаре).
_PLACEHOLDER_PRICE = 1_000_000


def _parse_listing(page_html: str, limit: int) -> list[Product]:
    products = []
    for m in _ITEM_RE.finditer(page_html):
        href_m = _HREF_RE.search(page_html, m.end(), m.end() + 500)
        if not href_m:
            continue
        price = int(float(m.group("price")))
        if price <= 0 or price == _PLACEHOLDER_PRICE:
            continue
        products.append(
            Product(
                shop="sulpak",
                name=html.unescape(m.group("name").strip()),
                price=price,
                currency="KZT",
                url=PRODUCT_URL.format(path=href_m.group("href")),
                specs={"brand": m.group("brand")},
                external_id=href_m.group("href"),
            )
        )
        if len(products) >= limit:
            break
    return products


class SulpakAdapter(ShopAdapter):
    name = "sulpak"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        data = await post_json(
            SEARCH_URL,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"https://www.sulpak.kz/Search?query={quote(query)}",
            },
            params={"query": query},
            data=_SEARCH_BODY,
        )
        return _parse_listing(data.get("products", ""), limit)

    async def fetch_price(self, external_id: str) -> Product | None:
        page_html = await get_text(PRODUCT_URL.format(path=external_id))
        name_m = re.search(r'data-name="([^"]+)"', page_html)
        price_m = re.search(r'data-price="([^"]+)"', page_html)
        if not name_m or not price_m:
            return None
        price = int(float(price_m.group(1)))
        if price <= 0 or price == _PLACEHOLDER_PRICE:
            return None
        return Product(
            shop="sulpak",
            name=html.unescape(name_m.group(1).strip()),
            price=price,
            currency="KZT",
            url=PRODUCT_URL.format(path=external_id),
            external_id=external_id,
        )
