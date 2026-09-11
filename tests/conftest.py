"""Общая настройка тестов.

Проект не установлен как пакет — модули (deals, db, config, adapters.*, ...)
импортируются по абсолютным именам из корня репозитория. При запуске через
`python -m pytest tests -q` из корня Python и так добавляет текущую директорию
в sys.path, но на случай запуска другим способом (IDE, `pytest tests`) делаем
это явно и здесь, чтобы импорт не зависел от способа запуска.
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
