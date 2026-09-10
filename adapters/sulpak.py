import asyncio
import html
import logging
import re
from urllib.parse import quote, urlencode

from adapters.base import ShopAdapter
from config import SULPAK_CITY_ID, SULPAK_LANGUAGE_ID
from http_client import get_text, post_json, post_text
from models import DiscountedUnit, Product

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.sulpak.kz/Search/LoadProducts/1/77/~/~/~/~/~/PopularityDesc/tiles"
PRODUCT_URL = "https://www.sulpak.kz{path}"
DISCOUNTED_INDEX_URL = "https://www.sulpak.kz/Discounted"
DISCOUNTED_LIST_URL = (
    "https://www.sulpak.kz/DiscountedProducts/{page}/{cls}/~/~/~/~/~/PopularityDesc/default"
)
CHEAPER_URL = "https://www.sulpak.kz/Goods/LoadCheaperPage"

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

# Уценка привязана к городу: и список товаров, и адрес склада приходят разные
# (проверено вживую — Алматы и Костанай отдают разные позиции).
_CITY_COOKIES = {"city_id": str(SULPAK_CITY_ID), "language_id": str(SULPAK_LANGUAGE_ID)}

_OLD_PRICE_RE = re.compile(r'product__item-price-old[^>]*>\s*([\d\s  ]+)')
_CATEGORY_RE = re.compile(r"/DiscountedProducts/1/(\d+)/")
_FULL_PRICE_RE = re.compile(r'data-full-price="([\d.]+)"')
_MAIN_IMAGE_RE = re.compile(r'data-main-image-url="([^"]*)"')

_UNIT_SPLIT_RE = re.compile(r'<div class="cart__additional-product">')
_UNIT_NAME_RE = re.compile(r'cart__additional-product-name">\s*([^<]+)')
_UNIT_PRICE_RE = re.compile(r'cart__additional-product-price">\s*([\d\s  ]+)')
_UNIT_DISCOUNT_RE = re.compile(r'product__main__label-discount">\s*([^<\s]+)')
_UNIT_DOC_RE = re.compile(r'data-document="([^"]+)"')
_UNIT_CITY_RE = re.compile(r'data-city-title="([^"]*)"')
_UNIT_MORE_RE = re.compile(
    r'product__cheaper-more-title">\s*(?P<title>[^<]+?)\s*:?\s*</div>\s*'
    r'<div class="product__cheaper-more-text">\s*(?P<text>[^<]*)',
    re.S,
)


def _digits(text: str) -> int | None:
    cleaned = re.sub(r"\D", "", text)
    return int(cleaned) if cleaned else None


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


def _parse_discounted_listing(page_html: str) -> list[dict]:
    """Карточки из раздела уценки. В отличие от поиска здесь важна ещё и
    зачёркнутая цена — она лежит не в data-атрибутах, а ниже по разметке
    карточки, поэтому режем HTML по границам карточек."""
    matches = list(_ITEM_RE.finditer(page_html))
    items = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page_html)
        segment = page_html[m.end():end]
        href_m = _HREF_RE.search(segment)
        if not href_m:
            continue
        price = int(float(m.group("price")))
        if price <= 0 or price == _PLACEHOLDER_PRICE:
            continue
        old_m = _OLD_PRICE_RE.search(segment)
        items.append({
            "name": html.unescape(m.group("name").strip()),
            "code": m.group("code"),
            "price": price,
            "old_price": _digits(old_m.group(1)) if old_m else None,
            "path": href_m.group("href").split("#")[0],
            "brand": m.group("brand"),
        })
    return items


def _parse_cheaper_units(fragment: str, item: dict, new_price: int | None) -> list[DiscountedUnit]:
    units = []
    for chunk in _UNIT_SPLIT_RE.split(fragment)[1:]:
        doc_m = _UNIT_DOC_RE.search(chunk)
        price_m = _UNIT_PRICE_RE.search(chunk)
        if not doc_m or not price_m:
            continue
        price = _digits(price_m.group(1))
        if not price:
            continue
        defects = {
            html.unescape(m.group("title")).strip(): html.unescape(m.group("text")).strip()
            for m in _UNIT_MORE_RE.finditer(chunk)
        }
        address = defects.pop("Адрес", None)
        name_m = _UNIT_NAME_RE.search(chunk)
        discount_m = _UNIT_DISCOUNT_RE.search(chunk)
        city_m = _UNIT_CITY_RE.search(chunk)
        units.append(DiscountedUnit(
            document=html.unescape(doc_m.group(1)).strip(),
            code=item["code"],
            name=html.unescape(name_m.group(1)).strip() if name_m else item["name"],
            price=price,
            url=PRODUCT_URL.format(path=item["path"]),
            old_price=item.get("old_price"),
            new_price=new_price,
            defects=defects,
            address=address,
            city=html.unescape(city_m.group(1)).strip() if city_m else None,
            shop_discount=discount_m.group(1).strip() if discount_m else None,
        ))
    return units


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

    async def discounted_categories(self) -> list[str]:
        page_html = await get_text(DISCOUNTED_INDEX_URL, cookies=_CITY_COOKIES)
        return list(dict.fromkeys(_CATEGORY_RE.findall(page_html)))

    async def discounted_items(self, category: str, max_pages: int = 20) -> list[dict]:
        """Все уценённые позиции категории в текущем городе, с пагинацией."""
        items: list[dict] = []
        seen_codes: set[str] = set()
        for page in range(1, max_pages + 1):
            page_html = await get_text(
                DISCOUNTED_LIST_URL.format(page=page, cls=category), cookies=_CITY_COOKIES
            )
            batch = _parse_discounted_listing(page_html)
            fresh = [i for i in batch if i["code"] not in seen_codes]
            if not fresh:
                break
            seen_codes.update(i["code"] for i in fresh)
            items += fresh
        return items

    async def discounted_units(self, item: dict) -> list[DiscountedUnit]:
        """Конкретные уценённые экземпляры одной модели: цена, дефекты, адрес.
        Живут не в листинге, а во вкладке «Уценённый товар» (AJAX-фрагмент),
        которой нужны данные со страницы товара."""
        product_url = PRODUCT_URL.format(path=item["path"])
        page_html = await get_text(product_url, cookies=_CITY_COOKIES)
        full_m = _FULL_PRICE_RE.search(page_html)
        image_m = _MAIN_IMAGE_RE.search(page_html)
        new_price = int(float(full_m.group(1))) if full_m else None
        if new_price == _PLACEHOLDER_PRICE:
            new_price = None

        body = urlencode({
            "code": item["code"],
            "productTitle": item["name"],
            "mainImageUrl": image_m.group(1) if image_m else "",
            "fullPrice": full_m.group(1) if full_m else "",
        })
        fragment = await post_text(
            CHEAPER_URL,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": product_url,
            },
            data=body,
            cookies=_CITY_COOKIES,
        )
        return _parse_cheaper_units(fragment, item, new_price)

    async def scan_discounted(self, concurrency: int = 4) -> list[DiscountedUnit]:
        """Полный обход раздела уценки по текущему городу."""
        categories = await self.discounted_categories()
        listings = await asyncio.gather(
            *(self.discounted_items(c) for c in categories), return_exceptions=True
        )
        items: list[dict] = []
        seen_codes: set[str] = set()
        for category, res in zip(categories, listings):
            if isinstance(res, Exception):
                log.warning("sulpak: категория уценки %s не открылась: %r", category, res)
                continue
            for item in res:
                if item["code"] not in seen_codes:
                    seen_codes.add(item["code"])
                    items.append(item)

        gate = asyncio.Semaphore(concurrency)

        async def load(item: dict) -> list[DiscountedUnit]:
            async with gate:
                try:
                    return await self.discounted_units(item)
                except Exception as exc:
                    log.warning("sulpak: уценка %s (%s) не загрузилась: %r",
                                item["name"], item["code"], exc)
                    return []

        batches = await asyncio.gather(*(load(i) for i in items))
        return [unit for batch in batches for unit in batch]

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
