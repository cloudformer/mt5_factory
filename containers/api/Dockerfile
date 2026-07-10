# 构建上下文 = repo 根目录 (需要打包 strategy_core)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY containers/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strategy_core/ strategy_core/
COPY containers/api/src/ src/
# schema 不进镜像: 由 compose 挂载 ./containers/postgres/schema → /app/schema
# (镜像只含代码, schema/src 全靠挂载+热重载, 改动无需 rebuild)

EXPOSE 8010

# --reload: src 是 volume 挂载, 代码变更自动重载 (与 web 的 gunicorn --reload 对齐);
# 没有它挂载形同虚设 —— make up 不重建未变的容器, 老进程会一直跑旧代码
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload"]
