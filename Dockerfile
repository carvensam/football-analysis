FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Hong_Kong \
    HOST=0.0.0.0 \
    PORT=10000 \
    DB_PATH=/app/football.db

WORKDIR /app

# 時區資料（slim 預設未必有）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 足球資料庫（約 111MB）：build 階段由 GitHub Release 下載焗入映像。
# 公開數據，直接寫死 URL（Render 不支援 build-args；寫死最穩陣）。
# 更新數據＝上新 release asset，改下面呢條 URL，push 後重新部署。
ENV DB_URL=https://github.com/carvensam/football-analysis/releases/download/db-20260920/football.db
RUN echo "Downloading database from $DB_URL" && \
      curl -fL --retry 3 -o /app/football.db "$DB_URL" && \
      ls -lh /app/football.db

EXPOSE 10000
CMD ["python", "app.py"]
