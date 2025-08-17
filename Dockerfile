# 🐍 Use a lightweight Python base image
FROM python:3.10-slim

# 📁 Set working directory
WORKDIR /app

# 📦 Install system dependencies required by Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libxss1 libappindicator1 libindicator7 fonts-liberation \
    libasound2 libatk-bridge2.0-0 libgtk-3-0 libcups2 libdrm-dev \
    libgbm-dev libnspr4 libwayland-client0 libwayland-egl1 libdbus-1-3 \
    libgdk-pixbuf2.0-0 libgconf-2-4 libgomp1 libjpeg-dev libwebp-dev \
    libtiff5 liblcms2-2 libpng-dev libxkbcommon0 libepoxy0 libva-wayland2 \
    libxcursor1 libxdamage1 libxrandr2 libexpat1 libfontconfig1 libfreetype6 \
    libharfbuzz-icu0 libharfbuzz0b libjpeg-turbo8 libpng16-16 libwebp6 \
    libxext6 libxfixes3 libxi6 libxrender1 libxtst6 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# 📜 Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🧭 Install Chromium browser for Playwright
RUN playwright install chromium

# 🧾 Copy application code
COPY . .

# 🚪 Expose the port used by FastAPI
EXPOSE 8000

# 🚀 Start the FastAPI app with Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
