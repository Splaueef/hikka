#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPOSITORY="https://github.com/Splaueef/hikka.git"
readonly PORT="${EXTERNAL_PORT:-3429}"

if [[ ! -f docker-compose.yml ]]; then
    if [[ -e Hikka ]]; then
        printf 'Hikka already exists, but is not a usable checkout.\n' >&2
        exit 1
    fi

    git clone --depth 1 "$REPOSITORY" Hikka
    cd Hikka
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    printf 'Docker Engine with the Compose plugin is required: https://docs.docker.com/engine/install/\n' >&2
    exit 1
fi

umask 077
printf 'EXTERNAL_PORT=%s\n' "$PORT" >.env

printf 'Building and starting Hikka...\n'
docker compose up --detach --build

printf '\nLocal setup: http://127.0.0.1:%s\n' "$PORT"
printf 'Waiting for the temporary HTTPS login URL (it is also available in docker compose logs)...\n'

for _ in {1..30}; do
    if url="$(docker compose logs --no-color worker 2>&1 | sed -nE 's/.*(https:\/\/[^[:space:]]+\.(lhr\.life|localhost\.run)).*/\1/p' | tail -n 1)" && [[ -n "$url" ]]; then
        printf 'Remote setup: %s\n' "$url"
        exit 0
    fi
    sleep 1
done

printf 'The tunnel is still starting. Follow it with: docker compose logs --follow worker\n' >&2
