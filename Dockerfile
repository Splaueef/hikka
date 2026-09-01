FROM python:3.11-slim

ENV DOCKER=true
ENV GIT_PYTHON_REFRESH=quiet

ENV PIP_NO_CACHE_DIR=1 \
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

# Окремий непривілейований користувач
RUN useradd -m -u 10001 -s /bin/bash hikka

RUN mkdir -p /data/Hikka \
    && chown -R hikka:hikka /data

WORKDIR /data/Hikka

# Спочатку залежності — Docker зможе кешувати цей шар
COPY --chown=hikka:hikka requirements.txt ./requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install \
        --no-warn-script-location \
        --no-cache-dir \
        -r requirements.txt

# Копіюємо код Hikka
COPY --chown=hikka:hikka . .

USER hikka

EXPOSE 8080

CMD ["python", "-m", "hikka"]




