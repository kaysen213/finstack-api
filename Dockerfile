FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
ENV ENV=production

EXPOSE 8000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 30
