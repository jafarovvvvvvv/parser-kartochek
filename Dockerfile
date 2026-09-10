# База с уже установленным Chromium для Playwright — понадобится адаптерам
# Kaspi/Ozon (сейчас пока заглушки, но интерфейс на это рассчитан).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# ffmpeg — для перекодировки голосовых сообщений Telegram (OGG/Opus) в mp3
# перед распознаванием через OpenAI.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
