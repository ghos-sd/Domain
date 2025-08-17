# صورة بايثون خفيفة
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# تثبيت متطلبات النظام الأساسية
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# مجلد العمل
WORKDIR /app

# نسخ واعتماد التبعيات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ الكود
COPY . .

# Railway يمرر PORT تلقائيًا، نخليه كمتغير بيئي
ENV PORT=8000

# أمر التشغيل
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
