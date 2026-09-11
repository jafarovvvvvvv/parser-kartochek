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

log = logging.getLogger("main")

# Пауза перед перезапуском упавшего воркера: без неё баг, из-за которого воркер
# падает сразу на старте (например, недоступна БД), крутил бы перезапуск в
# тугом цикле и забивал лог/CPU; 30 секунд достаточно, чтобы не спамить, и
# не настолько много, чтобы бот заметно "молчал".
RESTART_PAUSE_SEC = 30


async def _supervise(name: str, run) -> None:
    """Держит воркер живым вечно: если из него всё же вылетело необработанное
    исключение (scheduler.loop и deals.loop уже ловят свои ошибки внутри
    собственного while-цикла — это просто второй, внешний рубеж), падение
    логируется и воркер перезапускается после паузы, а не тянет за собой два
    остальных через asyncio.gather."""
    while True:
        try:
            await run()
        except Exception:
            log.exception("воркер %s упал, перезапуск через %sс", name, RESTART_PAUSE_SEC)
        else:
            # Воркеры — это `while True`, в норме они не возвращаются. Если
            # всё же вернулся без исключения, тоже не крутим цикл вхолостую.
            log.warning("воркер %s завершился без ошибки, перезапуск через %sс", name, RESTART_PAUSE_SEC)
        await asyncio.sleep(RESTART_PAUSE_SEC)


async def main() -> None:
    db.init_db()
    await asyncio.gather(
        _supervise("poll_loop", poll_loop),
        _supervise("scheduler", scheduler.loop),
        _supervise("deals", deals.loop),
    )


if __name__ == "__main__":
    asyncio.run(main())
