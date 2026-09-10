import json
import re

from adapters.base import ShopAdapter
from adapters.playwright_client import stealth_page
from models import Product

SEARCH_URL = "https://www.dns-shop.kz/search/?q={query}"
PRODUCT_URL = "https://www.dns-shop.kz{path}"

_EXTRACT_CARDS_JS = """
() => Array.from(document.querySelectorAll('.catalog-product')).map(c => {
  const nameEl = c.querySelector('.catalog-product__name');
  const linkEl = c.querySelector('a.catalog-product__name');
  const ratingEl = c.querySelector('[data-brand-rating-modal]');
  const specs = Array.from(c.querySelectorAll('.catalog-product__spec')).map(s => {
    const label = s.querySelector('.catalog-product__spec-label');
    const value = s.querySelector('.catalog-product__spec-value');
    return [label ? label.textContent.trim().replace(/:$/, '') : null,
            value ? value.textContent.trim() : null];
  });
  return {
    name: nameEl ? nameEl.childNodes[0].textContent.trim() : null,
    href: linkEl ? linkEl.getAttribute('href') : null,
    ratingAttr: ratingEl ? ratingEl.getAttribute('data-brand-rating-modal') : null,
    text: c.innerText,
    specs,
  };
})
"""


def _parse_price(text: str) -> int | None:
    m = re.search(r'([\d\s ]{3,})\s*₸', text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_rating(attr: str | None) -> float | None:
    if not attr:
        return None
    m = re.search(r'"rating"\s*:\s*([\d.]+)', attr)
    return float(m.group(1)) if m else None


def _to_product(card: dict) -> Product | None:
    if not card.get("name") or not card.get("href"):
        return None
    price = _parse_price(card.get("text", ""))
    if price is None:
        return None
    specs = {label: value for label, value in card.get("specs", []) if label and value}
    return Product(
        shop="dns",
        name=card["name"],
        price=price,
        currency="KZT",
        url=PRODUCT_URL.format(path=card["href"]),
        rating=_parse_rating(card.get("ratingAttr")),
        specs=specs,
        external_id=card["href"],
    )


class DnsAdapter(ShopAdapter):
    name = "dns"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        async with stealth_page() as page:
            await page.goto(
                SEARCH_URL.format(query=query.replace(" ", "+")),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_selector(".catalog-product", timeout=15000)
                await page.wait_for_function(
                    "document.querySelector('.catalog-product').innerText.includes('₸')",
                    timeout=10000,
                )
            except Exception:
                pass
            cards = await page.evaluate(_EXTRACT_CARDS_JS)
        products = [p for p in (_to_product(c) for c in cards) if p]
        return products[:limit]

    async def fetch_price(self, external_id: str) -> Product | None:
        # Цена на странице товара доступна сразу в JSON-LD (schema.org/Product),
        # не нужно ждать ленивую AJAX-подгрузку виджета покупки.
        async with stealth_page() as page:
            await page.goto(PRODUCT_URL.format(path=external_id), wait_until="domcontentloaded", timeout=30000)
            try:
                raw = await page.eval_on_selector(
                    'script[type="application/ld+json"]', "el => el.textContent"
                )
            except Exception:
                return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        offers = data.get("offers") or {}
        price = offers.get("price")
        if not price:
            return None
        return Product(
            shop="dns",
            name=data.get("name", ""),
            price=int(price),
            currency=offers.get("priceCurrency", "KZT"),
            url=PRODUCT_URL.format(path=external_id),
            external_id=external_id,
        )
