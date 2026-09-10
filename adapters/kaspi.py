import re

from adapters.base import ShopAdapter
from adapters.playwright_client import stealth_page
from config import KASPI_CITY
from models import Product

SEARCH_URL = "https://kaspi.kz/shop/search/?text={query}&c={city}"
PRODUCT_URL = "https://kaspi.kz{path}"

# Витрина — SPA: код (см. shop/front бандл, ключ selectedCityCookieName)
# берёт город не из URL ?c=, а из cookie "kaspi.storefront.cookie.city",
# и при её отсутствии молча падает на захардкоженный defaultCityId="750000000"
# (Алматы) — сам параметр ?c= в URL мы оставляем на всякий случай (может
# использоваться где-то на сервере), но полагаться только на него нельзя.
_CITY_COOKIE_NAME = "kaspi.storefront.cookie.city"


async def _set_city_cookie(page) -> None:
    await page.context.add_cookies([{
        "name": _CITY_COOKIE_NAME,
        "value": KASPI_CITY,
        "domain": "kaspi.kz",
        "path": "/",
    }])

_EXTRACT_CARDS_JS = """
() => Array.from(document.querySelectorAll('.item-card')).map(c => {
  const nameEl = c.querySelector('.item-card__name-link');
  const priceEl = c.querySelector('.item-card__prices-price');
  const ratingEl = c.querySelector('.rating');
  const imgEl = c.querySelector('.item-card__image img');
  return {
    name: nameEl ? nameEl.textContent.trim() : null,
    href: nameEl ? nameEl.getAttribute('href') : null,
    price: priceEl ? priceEl.textContent.trim() : null,
    ratingClass: ratingEl ? ratingEl.className : null,
    image: imgEl ? imgEl.getAttribute('src') : null,
  };
})
"""

_EXTRACT_SPECS_JS = """
() => Array.from(document.querySelectorAll('.specifications-list__spec')).map(el => {
  const term = el.querySelector('.specifications-list__spec-term-text, .specifications-list__term');
  const def = el.querySelector('.specifications-list__spec-definition');
  return [term ? term.textContent.trim() : null, def ? def.textContent.trim() : null];
})
"""


def _parse_price(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_rating(class_name: str | None) -> float | None:
    if not class_name:
        return None
    m = re.search(r"_(\d{2})\b", class_name)
    return int(m.group(1)) / 10 if m else None


def _to_product(card: dict) -> Product | None:
    price = _parse_price(card.get("price"))
    if not card.get("name") or not card.get("href") or price is None:
        return None
    return Product(
        shop="kaspi",
        name=card["name"],
        price=price,
        currency="KZT",
        url=PRODUCT_URL.format(path=card["href"]),
        image=card.get("image"),
        rating=_parse_rating(card.get("ratingClass")),
        external_id=card["href"],
    )


class KaspiAdapter(ShopAdapter):
    name = "kaspi"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        async with stealth_page() as page:
            await _set_city_cookie(page)
            url = SEARCH_URL.format(query=query.replace(" ", "%20"), city=KASPI_CITY)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(".item-card", timeout=10000)
            except Exception:
                return []
            cards = await page.evaluate(_EXTRACT_CARDS_JS)
        products = [p for p in (_to_product(c) for c in cards) if p]
        return products[:limit]

    async def fetch_price(self, external_id: str) -> Product | None:
        async with stealth_page() as page:
            await _set_city_cookie(page)
            await page.goto(PRODUCT_URL.format(path=external_id), wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(".item__price-once", timeout=10000)
            except Exception:
                return None
            name = await page.eval_on_selector(".item__heading", "el => el.textContent.trim()")
            price_text = await page.eval_on_selector(".item__price-once", "el => el.textContent")
        price = _parse_price(price_text)
        if price is None:
            return None
        return Product(
            shop="kaspi",
            name=name,
            price=price,
            currency="KZT",
            url=PRODUCT_URL.format(path=external_id),
            external_id=external_id,
        )

    async def fetch_specs(self, external_id: str) -> dict[str, str]:
        """Полная таблица характеристик со страницы товара (для ИИ-сравнения)."""
        async with stealth_page() as page:
            await _set_city_cookie(page)
            await page.goto(PRODUCT_URL.format(path=external_id), wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(".specifications-list__spec", timeout=8000)
            except Exception:
                return {}
            pairs = await page.evaluate(_EXTRACT_SPECS_JS)
        return {term: value for term, value in pairs if term and value}
