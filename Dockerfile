FROM python:3.11-slim

ENV DOCKER=true \
    GIT_PYTHON_REFRESH=quiet \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    git \
    build-essential \
    ffmpeg \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    /var/cache/apt/archives/* \
    /tmp/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hikka

RUN install -d -o hikka -g hikka /data /app

WORKDIR /app

COPY --chown=hikka:hikka requirements.txt ./requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install \
        --no-warn-script-location \
        --no-cache-dir \
        -r requirements.txt

COPY --chown=hikka:hikka . .

USER hikka

EXPOSE 8080

CMD ["python", "-m", "hikka", "--proxy-pass", "--no-tty"]




