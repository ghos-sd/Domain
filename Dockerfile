# استخدم صورة جاهزة فيها Chromium و Playwright مثبت مسبقاً
FROM mcr.microsoft.com/playwright/python:v1.43.1-jammy

# عيّن مجلد العمل
WORKDIR /app

# نسخ المتطلبات وتثبيت المكتبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ الكود إلى الحاوية
COPY . .

# فتح البورت للتطبيق
EXPOSE 8000

# تشغيل تطبيق FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
