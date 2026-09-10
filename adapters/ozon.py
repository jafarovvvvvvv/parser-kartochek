import logging

from adapters.base import ShopAdapter
from adapters.playwright_client import stealth_page
from models import Product

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.ozon.ru/search/?text={query}"

# Не подтверждено вживую (см. блок ниже) — по общедоступным упоминаниям
# устройства Ozon (data-widget="webProductHeading"/"webPrice") в карточке
# товара. Требует проверки на чистом IP и почти наверняка потребует правок.
_EXTRACT_CARDS_JS = """
() => Array.from(document.querySelectorAll('[data-widget="searchResultsV2"] a[href*="/product/"]')).map(a => {
  const card = a.closest('div');
  return {
    href: a.getAttribute('href'),
    text: card ? card.textContent : null,
  };
})
"""


class BlockedByCaptcha(Exception):
    """Ozon показал капчу вместо результатов поиска."""


def _is_captcha(title: str) -> bool:
    lowered = title.lower()
    return "captcha" in lowered or "не робот" in lowered or "not a robot" in lowered


class OzonAdapter(ShopAdapter):
    """
    Даже с headless Chromium (реалистичный UA/viewport, скрытый
    navigator.webdriver) Ozon показывает интерактивную капчу ("Antibot
    Captcha") на /search/ — проверено вживую из этой среды, включая
    /robots.txt, что говорит о блокировке на уровне IP дата-центра, а не
    только фингерпринта браузера. Автоматически проходить капчу мы не
    станем — это осознанное решение, не техническое ограничение.

    Код ниже реально ходит в браузер и честно детектит капчу, поэтому
    на другом (не дата-центровом) IP можно быстро проверить, снимается ли
    блокировка — но селекторы извлечения карточек НЕ проверены на живой
    выдаче (см. _EXTRACT_CARDS_JS) и почти наверняка потребуют правки.
    """

    name = "ozon"

    async def search(self, query: str, limit: int = 5) -> list[Product]:
        async with stealth_page() as page:
            url = SEARCH_URL.format(query=query.replace(" ", "+"))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            title = await page.title()
            if _is_captcha(title):
                log.warning("ozon: заблокировано капчей (%s)", title)
                raise BlockedByCaptcha(title)
            try:
                cards = await page.evaluate(_EXTRACT_CARDS_JS)
            except Exception:
                log.exception("ozon: не удалось распарсить карточки")
                return []
        log.info("ozon: страница прошла без капчи, но парсинг карточек не проверен (%d элементов)", len(cards))
        return []

    async def fetch_price(self, external_id: str) -> Product | None:
        return None
