FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تأكيد وجود Chromium
RUN playwright install chromium

COPY app.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["bash", "-lc", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
