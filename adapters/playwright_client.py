import asyncio
import random
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, async_playwright

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
"""

_playwright = None
_browser: Browser | None = None
# Защищает запуск браузера от гонки: поиск и обход уценки могут одновременно
# увидеть _browser is None и запустить по своему Chromium — лишний осиротеет
# и будет держать память в контейнере, который живёт неделями.
_browser_lock = asyncio.Lock()

# Пул готовых browser context на переиспользование. Context — не сам браузер
# (тот всегда один), а более лёгкая "профиль"-обвязка вокруг него, но её
# создание на каждый вызов (new_context + add_init_script + route — несколько
# круговых CDP-вызовов) всё равно заметная доля времени, когда Kaspi и DNS
# дёргаются по многу раз подряд (поиск, группировка, сравнение уценки).
# Пул НЕ блокирует: если он пуст, context создаётся как раньше, а лишний при
# возврате просто закрывается — переиспользование может только ускорить,
# а не подвесить вызывающего в очереди.
_MAX_POOL = 4
_context_pool: list[BrowserContext] = []


def _alive(browser: Browser | None) -> bool:
    return browser is not None and browser.is_connected()


async def _get_browser() -> Browser:
    global _playwright, _browser
    if not _alive(_browser):
        async with _browser_lock:
            # Двойная проверка: пока ждали лок, другой вызов уже мог поднять браузер.
            if not _alive(_browser):
                # Chromium в контейнере может умереть (OOM, крэш) — раньше тогда
                # _browser оставался заданным, и каждый следующий вызов падал до
                # перезапуска контейнера. Контексты из пула принадлежат мёртвому
                # процессу, их выкидываем.
                _context_pool.clear()
                if _playwright is None:
                    _playwright = await async_playwright().start()
                _browser = await _playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
    return _browser


async def _new_context(browser: Browser) -> BrowserContext:
    viewport = {"width": random.randint(1280, 1440), "height": random.randint(800, 900)}
    context = await browser.new_context(user_agent=_UA, locale="ru-RU", viewport=viewport)
    await context.add_init_script(_STEALTH_JS)
    await context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "font", "media")
        else route.continue_(),
    )
    return context


async def _acquire_context() -> BrowserContext:
    # Между проверкой и pop() нет await — цикл событий не может вклиниться
    # сюда с другим вызовом, поэтому отдельный лок для пула не нужен.
    while _context_pool:
        context = _context_pool.pop()
        if _alive(context.browser):
            return context
    browser = await _get_browser()
    return await _new_context(browser)


async def _release_context(context: BrowserContext) -> None:
    if len(_context_pool) < _MAX_POOL and _alive(context.browser):
        _context_pool.append(context)
    else:
        await _close_quietly(context)


async def _close_quietly(context: BrowserContext) -> None:
    try:
        await context.close()
    except Exception:
        pass  # контекст мог умереть вместе с браузером — закрывать уже нечего


@asynccontextmanager
async def stealth_page():
    context = await _acquire_context()
    healthy = False
    try:
        # Кука города Kaspi — единственное состояние, которое адаптеры пишут
        # поверх выданного context. Чистим перед выдачей страницы, чтобы
        # сессия/кука предыдущего вызова не утекла в следующий, кому этот же
        # context достанется из пула (пока context занят, в пул он не попадёт,
        # так что гонки с параллельным вызовом здесь нет).
        await context.clear_cookies()
        page = await context.new_page()
        try:
            yield page
            healthy = True
        finally:
            try:
                await page.close()
            except Exception:
                healthy = False
    finally:
        # Если вызов закончился исключением, контекст мог остаться в
        # непредсказуемом состоянии (зависшая навигация, умерший рендерер) —
        # в пул такой не возвращаем, иначе он отравит следующий вызов.
        if healthy:
            await _release_context(context)
        else:
            await _close_quietly(context)


async def shutdown() -> None:
    global _playwright, _browser
    for context in _context_pool:
        await context.close()
    _context_pool.clear()
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
