import os

BOT_TOKEN = os.environ.get("KARTOCHEK_BOT_TOKEN", "")
DB_PATH = os.environ.get("KARTOCHEK_DB_PATH", "kartochek.db")

# Казахстан по умолчанию: WB dest-набор для доставки в KZ (KZT), город — Костанай
# (цены Kaspi/Технодома различаются по городам — код города даёт актуальные
# для Костаная, а не алматинские).
WB_DEST = os.environ.get("WB_DEST", "12358388,12358411,-5513216,122")
KASPI_CITY = os.environ.get("KASPI_CITY", "391010000")  # Костанай
TECHNODOM_CITY_ID = os.environ.get("TECHNODOM_CITY_ID", "5f5f1e3b643d490d8a416164")  # Костанай
TECHNODOM_CITY_SLUG = os.environ.get("TECHNODOM_CITY_SLUG", "kostanay")

HTTP_TIMEOUT = float(os.environ.get("KARTOCHEK_HTTP_TIMEOUT", "10"))
HTTP_RETRIES = int(os.environ.get("KARTOCHEK_HTTP_RETRIES", "2"))

PRICE_CHECK_INTERVAL_MIN = int(os.environ.get("KARTOCHEK_PRICE_CHECK_INTERVAL_MIN", "180"))
MAX_CHECKS_PER_HOUR = int(os.environ.get("KARTOCHEK_MAX_CHECKS_PER_HOUR", "60"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# GPT-5.6 (07.2026) — три уровня: sol (флагман, сложные задачи), terra
# (средний), luna (быстрые/дешёвые типовые задачи вроде суммаризации и
# извлечения данных из текста — то, что нам и нужно для сравнения характеристик).
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
# gpt-transcribe (07.2026) — сменил whisper-1/gpt-4o-transcribe, дешевле и точнее.
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-transcribe")
