# خفيف ومستقر
FROM python:3.11-slim

# إعدادات عامة للبايثون
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# مجلد العمل
WORKDIR /app

# تنصيب باكدجات البايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تنزيل كروميوم وتبعياته تلقائياً (أسهل وأضمن من apt)
# --with-deps يثبت كل مكتبات النظام المطلوبة للتشغيل داخل الحاوية
RUN playwright install --with-deps chromium

# نسخ الكود
COPY . .

# البورت اللي هيخدم عليه Uvicorn
EXPOSE 8000

# أمر التشغيل
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
