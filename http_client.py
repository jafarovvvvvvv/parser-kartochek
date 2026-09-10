import asyncio
import json
import random
from urllib.parse import urlencode

import httpx

from config import HTTP_RETRIES, HTTP_TIMEOUT

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


class BlockedError(Exception):
    """Магазин вернул капчу/антибот-страницу вместо данных."""


def random_headers(extra: dict | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if extra:
        headers.update(extra)
    return headers


def looks_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 429):
        return True
    lowered = text[:2000].lower()
    return any(marker in lowered for marker in ("captcha", "не робот", "antibot", "access denied"))


async def _via_curl(method: str, url: str, headers: dict, data: str | None,
                     cookies: dict | None = None) -> str:
    """Резерв для эндпоинтов, блокирующих httpx по TLS-отпечатку (напр. card.wb.ru),
    но пропускающих curl. Не защита от полноценного антибота с капчей."""
    cmd = ["curl", "-s", "-m", str(int(HTTP_TIMEOUT)), "-w", "\n%{http_code}", "-X", method]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    if cookies:
        cmd += ["-b", "; ".join(f"{k}={v}" for k, v in cookies.items())]
    if data is not None:
        cmd += ["--data-raw", data]
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    body, _, status = text.rpartition("\n")
    if not status.isdigit() or looks_blocked(int(status), body):
        raise BlockedError(f"blocked by {url} (curl fallback, status={status})")
    return body


async def _request_text(method: str, url: str, *, headers: dict | None, params: dict | None,
                         data: str | None, cookies: dict | None = None) -> str:
    # Некоторые API маркетплейсов (например card.wb.ru) отдают 403 на %2C
    # вместо запятой в query, поэтому кодируем вручную, оставляя запятую как есть.
    if params:
        url = f"{url}?{urlencode(params, safe=',')}"
    headers = headers or random_headers()
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                  cookies=cookies) as client:
        for attempt in range(HTTP_RETRIES + 1):
            is_last_attempt = attempt == HTTP_RETRIES
            try:
                resp = await client.request(method, url, headers=headers, content=data)
                if looks_blocked(resp.status_code, resp.text):
                    raise BlockedError(f"blocked by {url} (status={resp.status_code})")
                resp.raise_for_status()
                return resp.text
            except BlockedError as exc:
                last_exc = exc
                # 429/403 нередко временные (рейт-лимит) — сначала пара
                # повторов с паузой, и только на последней попытке пробуем
                # curl (спасает от блокировки httpx по TLS-отпечатку, но не
                # от настоящего рейт-лимита/капчи).
                if not is_last_attempt:
                    await asyncio.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.5))
                    continue
                return await _via_curl(method, url, headers, data, cookies)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if not is_last_attempt:
                    await asyncio.sleep(0.8 * (attempt + 1) + random.uniform(0, 0.4))
    raise last_exc


async def get_text(url: str, *, headers: dict | None = None, params: dict | None = None,
                    cookies: dict | None = None) -> str:
    return await _request_text("GET", url, headers=headers, params=params, data=None,
                                cookies=cookies)


async def get_json(url: str, *, headers: dict | None = None, params: dict | None = None) -> dict | list:
    return json.loads(await _request_text("GET", url, headers=headers, params=params, data=None))


async def post_text(url: str, *, headers: dict | None = None, params: dict | None = None,
                     data: str | None = None, cookies: dict | None = None) -> str:
    return await _request_text("POST", url, headers=headers, params=params, data=data,
                                cookies=cookies)


async def post_json(url: str, *, headers: dict | None = None, params: dict | None = None,
                     data: str | None = None, json_body: dict | None = None,
                     cookies: dict | None = None) -> dict | list:
    if json_body is not None:
        data = json.dumps(json_body)
        headers = (headers or random_headers()) | {"Content-Type": "application/json"}
    return json.loads(await _request_text("POST", url, headers=headers, params=params, data=data,
                                           cookies=cookies))
