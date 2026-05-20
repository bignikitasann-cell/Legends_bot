FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости для gTTS и других библиотек
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PORT=8080

CMD exec python bot.py
