import json
import re
from urllib.parse import urlparse

from adapters.base import ShopAdapter
from adapters.playwright_client import stealth_page
from config import DNS_CITY_PATH
from models import DiscountedUnit, Product

SEARCH_URL = "https://www.dns-shop.kz/search/?q={query}"
PRODUCT_URL = "https://www.dns-shop.kz{path}"
DISCOUNTED_URL = "https://www.dns-shop.kz/catalog/markdown/"
# Пагинация подобрана вживую: постраничные ссылки в разметке рисует отдельный
# ajax-виджет (products-list-pagination), который, как и цену, приходится
# ждать отдельно — а вот сам параметр запроса можно дёргать напрямую.
# /catalog/markdown/page-2/ — 404, ?PAGEN_1=2 (обычный Bitrix) молча отдаёт
# страницу 1 заново — работает только ?p=N (в Костанае 26 позиций = 18 + 8,
# то есть ровно две страницы).
DISCOUNTED_PAGE_URL = DISCOUNTED_URL + "?p={page}"

_CITY_NAMES = {"kostanay": "Костанай"}

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

# Карточка уценки — тот же .catalog-product, плюс класс markdown-product и
# своя разметка (проверено вживую на /catalog/markdown/): нет
# data-brand-rating-modal и характеристик-пар, зато есть текст дефекта
# (.catalog-product__reasons-item) и попап с адресом магазина прямо в HTML
# карточки (без похода на страницу товара).
_EXTRACT_MARKDOWN_JS = """
() => Array.from(document.querySelectorAll('.catalog-product.markdown-product')).map(c => {
  const nameEl = c.querySelector('.catalog-product__name');
  const linkEl = c.querySelector('a.catalog-product__name');
  const imgEl = c.querySelector('.catalog-product__image img');
  const defectEl = c.querySelector('.catalog-product__reasons-item')
                 || c.querySelector('.catalog-product__specs-column:last-child .catalog-product__spec-value');
  const addrLinkEl = c.querySelector('.order-avail-wrap__link');
  const popoverSel = addrLinkEl ? addrLinkEl.getAttribute('data-content-target') : null;
  const popover = popoverSel ? document.querySelector(popoverSel) : null;
  let address = null;
  if (popover) {
    const addrP = Array.from(popover.querySelectorAll('p')).find(
      p => p.querySelector('b') && p.querySelector('b').textContent.trim().startsWith('Адрес')
    );
    if (addrP) {
      address = addrP.textContent.replace(/^\\s*Адрес:/, '').replace('показать на карте', '').trim();
    }
  }
  // Цена и зачёркнутая цена рисуются отдельным ajax-виджетом (.catalog-product__buy),
  // как и в обычном поиске — дожидаемся его в scan_discounted(). Разметка
  // проверена вживую (реальный Playwright, не догадка):
  // <div class="product-buy__price ...">20 490&nbsp;₸<span class="product-buy__prev">37 990</span></div>
  // — зачёркнутая цена лежит в дочернем .product-buy__prev и идёт БЕЗ ₸,
  // поэтому текст текущей цены берём только из прямых текстовых узлов
  // .product-buy__price (без вложенного span), а старую — отдельно из prev.
  const priceEl = c.querySelector('.product-buy__price');
  const prevEl = c.querySelector('.product-buy__prev');
  let currentText = priceEl
    ? Array.from(priceEl.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join('')
    : '';
  const oldText = prevEl ? prevEl.textContent : '';
  if (!currentText) currentText = c.innerText;
  return {
    name: nameEl ? nameEl.childNodes[0].textContent.trim() : null,
    href: linkEl ? linkEl.getAttribute('href') : null,
    code: c.getAttribute('data-code'),
    photo: imgEl ? (imgEl.getAttribute('data-src') || imgEl.getAttribute('src')) : null,
    defectText: defectEl ? defectEl.textContent.trim() : null,
    address,
    currentText,
    oldText,
  };
})
"""


# Цена в карточке DNS идёт отдельной строкой ("66 490\xa0₸"), а перед ней идёт
# последняя характеристика. \s в числе пускать нельзя: он ловит перевод строки
# и склеивает "IP54\n66 490" в 5466490 — поймано вживую на выдаче по airpods.
# Первое совпадение — актуальная цена, следующая строка с ₸ — уже "в рассрочку".
_PRICE_RE = re.compile(r'(\d[\d   ]*)[   ]*₸')


def _parse_price(text: str) -> int | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _parse_rating(attr: str | None) -> float | None:
    if not attr:
        return None
    m = re.search(r'"rating"\s*:\s*([\d.]+)', attr)
    return float(m.group(1)) if m else None


def _product_path(href: str) -> str:
    """href в карточке бывает и абсолютным (у блока аналогов) — тогда подстановка
    в PRODUCT_URL давала склейку вида "dns-shop.kzhttps://dns-shop.kz/..."
    и нерабочую ссылку в сообщении (поймано вживую)."""
    if href.startswith("http"):
        return urlparse(href).path
    return href


# Номера документа уценки, как у Sulpak, у DNS нет — зато у каждого экземпляра
# своя страница /catalog/markdown/<uuid>/, и этот uuid не меняется между
# обходами. Он и есть ключ дедупа (см. scan_discounted).
_MARKDOWN_ID_RE = re.compile(r'/catalog/markdown/([0-9a-f-]+)/?')

# "После ремонта( ... )" — единственная часть дефекта с постоянной структурой
# на всей выборке (после ремонта таких — примерно две трети из 26 позиций,
# проверено вживую): вырезаем в отдельный ключ, остальное — как есть, одним
# блоком, чтобы не выдумывать разбивку, которой на сайте нет.
_REPAIR_RE = re.compile(r'^После ремонта\(\s*(?P<detail>[^)]*?)\s*\)\s*,?\s*')


def _digits(text: str | None) -> int | None:
    """Зачёркнутая цена в виджете DNS (.product-buy__prev) идёт БЕЗ ₸ —
    "37 990", а не "37 990 ₸" (проверено вживую) — _parse_price её не возьмёт,
    он именно ждёт суффикс ₸. Поэтому для неё отдельный, более простой разбор:
    просто цифры, без риска зацепить перевод строки (тут его и негде — это
    текст одного-единственного span, не всей карточки)."""
    cleaned = re.sub(r"\D", "", text or "")
    return int(cleaned) if cleaned else None


def _parse_defects(text: str | None) -> dict[str, str]:
    text = (text or "").strip()
    defects: dict[str, str] = {}
    m = _REPAIR_RE.match(text)
    if m:
        defects["Ремонт"] = m.group("detail").strip()
        text = text[m.end():].strip(" ,")
    if text:
        defects["Состояние"] = text
    return defects


def _to_product(card: dict) -> Product | None:
    if not card.get("name") or not card.get("href"):
        return None
    price = _parse_price(card.get("text", ""))
    if price is None:
        return None
    specs = {label: value for label, value in card.get("specs", []) if label and value}
    path = _product_path(card["href"])
    return Product(
        shop="dns",
        name=card["name"],
        price=price,
        currency="KZT",
        url=PRODUCT_URL.format(path=path),
        rating=_parse_rating(card.get("ratingAttr")),
        specs=specs,
        external_id=path,
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

    async def _load_json_ld(self, external_id: str) -> list[str] | None:
        async with stealth_page() as page:
            await page.goto(PRODUCT_URL.format(path=external_id), wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(
                    'script[type="application/ld+json"]', state="attached", timeout=15000
                )
                return await page.evaluate(
                    "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
                    ".map(e => e.textContent)"
                )
            except Exception:
                return None

    async def fetch_price(self, external_id: str) -> Product | None:
        # Цена на странице товара есть в JSON-LD (schema.org/Product) — ждать
        # ленивую AJAX-подгрузку виджета покупки не нужно. Но сам JSON-LD в DOM
        # появляется не мгновенно: сразу после domcontentloaded его ещё нет, и
        # обновление цены молча возвращало None (поймано на живых товарах).
        # Блоков на странице бывает несколько (хлебные крошки и т.д.) — берём
        # именно Product. Промах бывает и плавающий (Cloudflare отдаёт страницу
        # не с первого раза), поэтому одна повторная попытка.
        blocks = await self._load_json_ld(external_id)
        if not blocks:
            blocks = await self._load_json_ld(external_id)
        data = None
        for raw in blocks or []:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("@type") == "Product":
                data = parsed
                break
        if data is None:
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

    async def _set_city(self, page) -> None:
        """Город DNS задаётся кукой city_path (у Sulpak — city_id), см.
        config.DNS_CITY_PATH. playwright_client чистит куки контекста перед
        каждой выдачей страницы (общий пул на все адаптеры), поэтому ставим
        куку заново на каждый вызов, а не один раз при инициализации."""
        await page.context.add_cookies([{
            "name": "city_path",
            "value": DNS_CITY_PATH,
            "domain": ".dns-shop.kz",
            "path": "/",
        }])

    async def _fetch_markdown_page(self, pageno: int) -> list[dict]:
        """Одна страница уценки. Открываем свой stealth_page() на каждую
        страницу, а не гоняем один и тот же page.goto() по кругу — проверено
        вживую: при повторной навигации в ОДНОМ page вторая страница (p=2)
        стабильно давала либо пустую выдачу карточек, либо карточки без цены
        (ajax-виджет не успевал), а с отдельным контекстом на страницу —
        грузится штатно каждый раз (похоже на анти-бот реакцию Cloudflare на
        быструю повторную навигацию в одной сессии страницы)."""
        async with stealth_page() as page:
            await self._set_city(page)
            await page.goto(
                DISCOUNTED_PAGE_URL.format(page=pageno), wait_until="domcontentloaded", timeout=30000
            )
            try:
                await page.wait_for_selector(".catalog-product.markdown-product", timeout=15000)
            except Exception:
                return []  # дальше страниц уже нет (пустая выдача)
            try:
                await page.wait_for_function(
                    "document.querySelector('.catalog-product.markdown-product').innerText.includes('₸')",
                    timeout=15000,
                )
            except Exception:
                pass
            return await page.evaluate(_EXTRACT_MARKDOWN_JS)

    async def scan_discounted(self, max_pages: int = 10) -> list[DiscountedUnit]:
        """Полный обход раздела уценки DNS (/catalog/markdown/) по текущему
        городу (config.DNS_CITY_PATH). В отличие от Sulpak список карточек
        целиком в серверном HTML — сразу с дефектами и адресом магазина,
        отдельный запрос за экземплярами не нужен, — но постранично (по 18
        штук, см. DISCOUNTED_PAGE_URL), и цену, как и в search(), всё равно
        приходится ждать отдельным ajax-виджетом.

        new_price (цена нового такого же) отдельным походом на страницу
        товара не добывается: проверено вживую на нескольких находках —
        .product-buy__prev (зачёркнутая цена на карточке уценки) совпадает
        ровно с текущей ценой варианта "Новый" той же модели, добытой
        независимо через fetch_price/JSON-LD, и отсутствует ровно тогда,
        когда у модели такого варианта уже нет. Значит это одна и та же
        живая величина, и лишний per-item заход на Cloudflare-защищённый
        сайт (26+ карточек) не нужен — что видно на листинге, то и берём."""
        cards: list[dict] = []
        for pageno in range(1, max_pages + 1):
            batch = await self._fetch_markdown_page(pageno)
            if not batch:
                break
            cards += batch

        items: list[dict] = []
        seen: dict[str, int] = {}
        for c in cards:
            if not c.get("name") or not c.get("href"):
                continue
            path = _product_path(c["href"])
            id_m = _MARKDOWN_ID_RE.search(path)
            base_id = id_m.group(1) if id_m else path.strip("/")
            # Защита от неожиданного дубля uuid в выдаче (не должно случаться,
            # но повторный алерт по той же находке хуже лишнего порядкового номера).
            n = seen.get(base_id, 0)
            seen[base_id] = n + 1
            document = f"dns:{base_id}" if n == 0 else f"dns:{base_id}:{n}"

            price = _parse_price(c.get("currentText") or "")
            if price is None:
                continue
            # Зачёркнутая цена (.product-buy__prev) без ₸ — отдельным разбором
            # (см. _digits), она же и "цена нового такого же" (см. докстринг выше).
            old_price = _digits(c.get("oldText"))
            # На уценке цена не может быть выше зачёркнутой — если получилось
            # наоборот, значит перепутали местами при разборе виджета (DNS не
            # показывает "скидку в минус"), а не нашли реальную аномалию.
            if old_price is not None and price > old_price:
                price, old_price = old_price, price

            items.append({
                "document": document,
                # Код модели нужен, чтобы собрать экземпляры одной модели в одно
                # сообщение. Без data-code пустая строка склеила бы в «одну
                # модель» все такие карточки подряд — тогда пусть лучше каждая
                # будет сама по себе.
                "code": c.get("code") or base_id,
                "name": c["name"],
                "price": price,
                "old_price": old_price,
                "new_price": old_price,
                "path": path,
                "defects": _parse_defects(c.get("defectText")),
                "address": c.get("address"),
                "photos": [c["photo"]] if c.get("photo") else [],
            })

        city = _CITY_NAMES.get(DNS_CITY_PATH, DNS_CITY_PATH)
        return [
            DiscountedUnit(
                shop="dns",
                document=i["document"],
                code=i["code"],
                name=i["name"],
                price=i["price"],
                url=PRODUCT_URL.format(path=i["path"]),
                old_price=i["old_price"],
                new_price=i["new_price"],
                defects=i["defects"],
                address=i["address"],
                city=city,
                photos=i["photos"],
            )
            for i in items
        ]
