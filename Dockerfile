FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./requirements.txt

RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/python -m pip install -r requirements.txt


FROM python:3.11-slim-bookworm

ENV DOCKER=true \
    GIT_PYTHON_REFRESH=quiet \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_TARGET=/data/python \
    PYTHONPATH=/data/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libcairo2 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hikka \
    && install -d -o hikka -g hikka /app /data /data/python

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=hikka:hikka . .

USER hikka

EXPOSE 8080

CMD ["python", "-m", "hikka", "--proxy-pass", "--no-tty"]




