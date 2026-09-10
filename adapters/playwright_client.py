import random
from contextlib import asynccontextmanager

from playwright.async_api import Browser, async_playwright

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


async def _get_browser() -> Browser:
    global _playwright, _browser
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
    return _browser


@asynccontextmanager
async def stealth_page():
    browser = await _get_browser()
    viewport = {"width": random.randint(1280, 1440), "height": random.randint(800, 900)}
    context = await browser.new_context(user_agent=_UA, locale="ru-RU", viewport=viewport)
    await context.add_init_script(_STEALTH_JS)
    await context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "font", "media")
        else route.continue_(),
    )
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


async def shutdown() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
