import asyncio
import logging

import db
import deals
import scheduler
from bot import poll_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx на INFO печатает URL каждого запроса, а в адресе Telegram API лежит токен
# бота — в логи контейнера ему не надо. Логи самих адаптеров это не трогает.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    db.init_db()
    await asyncio.gather(poll_loop(), scheduler.loop(), deals.loop())


if __name__ == "__main__":
    asyncio.run(main())
