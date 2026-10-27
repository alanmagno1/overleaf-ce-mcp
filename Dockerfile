FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv pip install --system --no-cache \
    "mcp[cli]>=1.9.0,<2.0.0" \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "httpx>=0.27.0" \
    "pymongo>=4.9.0" \
    "redis>=5.2.0" \
    "python-multipart>=0.0.12" \
    && useradd -m -u 1000 app

WORKDIR /app
COPY src/server.py /app/server.py

USER app
ENV HOME=/home/app
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
