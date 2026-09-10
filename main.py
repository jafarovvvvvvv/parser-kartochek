import asyncio
import logging

import db
import scheduler
from bot import poll_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> None:
    db.init_db()
    await asyncio.gather(poll_loop(), scheduler.loop())


if __name__ == "__main__":
    asyncio.run(main())
