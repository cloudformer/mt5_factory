FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --timeout 300: 评定工具页(Regime候选对比)现算8口径×全品种, 千万行级数据下超默认30秒
# 会被 gunicorn 杀 worker(2026-07-29 实证"页面打不开"); 普通页面毫秒级不受影响
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "2", "--timeout", "300", "--reload", "app:app"]
