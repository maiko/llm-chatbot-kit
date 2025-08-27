# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
COPY personalities ./personalities
RUN python -m pip install --upgrade pip wheel setuptools && python -m pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN groupadd -g 1000 app && useradd -m -u 1000 -g 1000 -s /bin/bash app
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY personalities ./personalities
RUN mkdir -p /data && chown -R app:app /data /app
ENV XDG_CACHE_HOME=/data \
    HOME=/home/app
VOLUME ["/data"]
USER app
CMD ["llm-chatbot", "discord", "run", "--personality", "personalities/aelita.yml"]
