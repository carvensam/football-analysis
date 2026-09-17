FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Hong_Kong \
    HOST=0.0.0.0 \
    PORT=10000

WORKDIR /app

# 時區資料（slim 預設未必有）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 足球資料庫（約 111MB）：build 階段由 GitHub Release 下載（DB_URL 由 render.yaml 提供）
# 更新數據＝上傳新 release asset，改 render.yaml 嘅 DB_URL，然後重新部署
RUN test -n "$DB_URL" && \
      echo "Downloading database from $DB_URL" && \
      curl -fL --retry 3 -o /app/football.db "$DB_URL" && \
      ls -lh /app/football.db

EXPOSE 10000
CMD ["python", "app.py"]
