# 消防圖審系統 API（ARM64／x86 皆可建）
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY data/lawdb ./data/lawdb
COPY data/tables ./data/tables
# 以無家目錄的帳號執行；ezdxf 等套件的快取放暫存區
ENV XDG_CACHE_HOME=/tmp/.cache
USER 65534:65534
EXPOSE 8000
CMD ["uvicorn", "litian.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
