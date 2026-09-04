# dify-offline-repack Web 服务容器化
# 构建：docker build -f dify-offline-web/Dockerfile -t dify-offline-repack-web .
# 运行：docker run -p 8010:8010 -v dify-offline-data:/app/data dify-offline-repack-web
ARG BASE=python:3.12-slim
FROM ${BASE}

# 引擎与 Web 依赖（uv 用于依赖锁定/解析；cryptography 用于自签）
RUN pip install --no-cache-dir uv "uvicorn[standard]" fastapi python-multipart requests \
    PyYAML tomli-w cryptography && rm -rf /root/.cache/pip

WORKDIR /app
COPY dify-offline-repack /app/dify-offline-repack
COPY dify-offline-web /app/dify-offline-web

ENV DIFY_OFFLINE_ENGINE=/app/dify-offline-repack \
    DIFY_OFFLINE_DATA=/app/data \
    PYTHONUNBUFFERED=1

VOLUME /app/data
EXPOSE 8010
WORKDIR /app/dify-offline-web
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
