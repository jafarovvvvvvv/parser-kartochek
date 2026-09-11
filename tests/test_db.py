"""db.py на временной базе (KARTOCHEK_DB_PATH выставляется на временный файл
ДО импорта db, т.к. config читает окружение при импорте — здесь это делается
через importlib.reload, чтобы не зависеть от порядка сбора тестов pytest).

Покрывает:
- цикл добавить в отслеживание -> снять -> добавить снова не должен падать
  (была ошибка с получением id после ON CONFLICT, ронявшая запись истории
  цен по внешнему ключу);
- init_db на "старой" базе без миграционных колонок seen_deals (shop, photos,
  url) должен их доложить, а повторный вызов init_db не должен падать.
"""
import importlib
import os

import pytest


@pytest.fixture()
def db_module(tmp_path):
    db_path = tmp_path / "test_kartochek.db"
    os.environ["KARTOCHEK_DB_PATH"] = str(db_path)
    try:
        import config
        importlib.reload(config)
        import db
        importlib.reload(db)
        db.init_db()
        yield db
    finally:
        os.environ.pop("KARTOCHEK_DB_PATH", None)


def test_цикл_track_untrack_track_не_падает_по_внешнему_ключу(db_module):
    chat_id = 1
    shop = "wildberries"
    external_id = "12345"
    name = "Тестовый товар"

    item_id_1 = db_module.add_tracked_item(
        chat_id, shop, external_id, name, "http://example.com", "KZT", 1000
    )
    assert db_module.deactivate_tracked_item(item_id_1, chat_id) is True

    # Повторное добавление того же товара идёт через ветку ON CONFLICT DO
    # UPDATE — id должен вернуться тот же самый, а запись price_history не
    # должна упасть по внешнему ключу tracked_item_id.
    item_id_2 = db_module.add_tracked_item(
        chat_id, shop, external_id, name, "http://example.com", "KZT", 1200
    )
    assert item_id_2 == item_id_1

    active = db_module.list_tracked_items_for_chat(chat_id)
    assert len(active) == 1
    assert active[0]["active"] == 1

    # Третий цикл: снять и добавить снова — тоже без падения.
    assert db_module.deactivate_tracked_item(item_id_2, chat_id) is True
    item_id_3 = db_module.add_tracked_item(
        chat_id, shop, external_id, name, "http://example.com", "KZT", 900
    )
    assert item_id_3 == item_id_1

    with db_module.connect() as conn:
        history = conn.execute(
            "SELECT * FROM price_history WHERE tracked_item_id = ?", (item_id_1,)
        ).fetchall()
    # Одна запись истории на каждое добавление в отслеживание (3 цикла).
    assert len(history) == 3


def test_init_db_на_старой_схеме_добавляет_недостающие_колонки(db_module):
    # Имитируем базу до миграций: seen_deals без shop/photos/url.
    with db_module.connect() as conn:
        conn.execute("DROP TABLE seen_deals")
        conn.execute(
            """
            CREATE TABLE seen_deals (
                document TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                discount INTEGER NOT NULL,
                notified_at INTEGER NOT NULL
            )
            """
        )

    # init_db должен доложить недостающие колонки и не упасть от повторного
    # вызова (миграции идемпотентны).
    db_module.init_db()
    db_module.init_db()

    with db_module.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(seen_deals)")}
    assert {"shop", "photos", "url"} <= columns

    # После миграции запись находки с фото не должна падать из-за старой схемы.
    db_module.mark_deal_notified(
        "DOC1", "dns", "CODE1", "Тестовый товар", 1000, 50,
        "http://example.com/item", ["http://cdn.example.com/1.jpg"],
    )
    assert db_module.deal_photos("DOC1") == ["http://cdn.example.com/1.jpg"]


def test_init_db_повторный_вызов_на_обычной_базе_не_падает(db_module):
    # init_db уже был вызван фикстурой при создании базы — повторный вызов
    # на нормальной (не "устаревшей") схеме тоже должен быть безопасным.
    db_module.init_db()
    db_module.init_db()
