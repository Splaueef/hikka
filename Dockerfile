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
    HIKKA_REPOSITORY=https://github.com/Splaueef/hikka.git \
    GIT_PYTHON_REFRESH=quiet \
    PATH=/data/python/bin:/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_TARGET=/data/python \
    PYTHONPATH=/data/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    curl \
    ffmpeg \
    git \
    libcairo2 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hikka \
    && install -d -o hikka -g hikka /data /data/app /data/python

COPY --from=builder /opt/venv /opt/venv

WORKDIR /opt/hikka
COPY --chown=hikka:hikka . .
COPY --chown=hikka:hikka docker-entrypoint.sh /usr/local/bin/hikka-entrypoint

RUN chmod 755 /usr/local/bin/hikka-entrypoint

USER hikka

EXPOSE 8080

ENTRYPOINT ["hikka-entrypoint"]
CMD ["python", "-m", "hikka", "--proxy-pass", "--no-tty"]




