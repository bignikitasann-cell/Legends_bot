FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Используем порт из переменной окружения Render
ENV PORT=8080

CMD exec python bot.py
