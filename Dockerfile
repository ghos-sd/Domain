# صورة Playwright الرسمية (تجي جاهزة بكل الديبندنسيز والمتصفحات)
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# إعدادات بايثون
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# مجلد العمل
WORKDIR /app

# المتطلبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# الكود
COPY . .

# المنفذ
EXPOSE 8000

# تشغيل السيرفر
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
