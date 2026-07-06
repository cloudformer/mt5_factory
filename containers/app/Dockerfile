# 构建上下文 = repo 根目录 (需要打包 strategy_core)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY containers/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strategy_core/ strategy_core/
COPY containers/app/src/ src/

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
