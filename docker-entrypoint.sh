#!/usr/bin/env bash
set -Eeuo pipefail

readonly app_dir="${HIKKA_APP_DIR:-/data/app}"
readonly seed_dir="/opt/hikka"
readonly repository="${HIKKA_REPOSITORY:-https://github.com/Splaueef/hikka.git}"

mkdir -p "$app_dir" /data/python

# Keep the runnable checkout on the persistent volume.  Consequently both code
# downloaded by .update and dependencies installed by modules survive a
# container recreation.
if [[ ! -d "$app_dir/.git" ]]; then
    cp -a "$seed_dir/." "$app_dir/"
fi

if [[ -d "$app_dir/.git" ]]; then
    git -C "$app_dir" remote set-url origin "$repository" 2>/dev/null \
        || git -C "$app_dir" remote add origin "$repository"

    # Updating must not make an otherwise healthy container unbootable when
    # GitHub (or DNS) is temporarily unavailable.
    if ! git -C "$app_dir" pull --ff-only --quiet; then
        printf 'Unable to update Hikka; starting the persisted version.\n' >&2
    fi
fi

# PIP_TARGET points at this persistent directory. Reinstalling here also picks
# up requirements changed by the pull above without modifying the base image.
python -m pip install --upgrade --disable-pip-version-check \
    --no-warn-script-location --quiet -r "$app_dir/requirements.txt"

cd "$app_dir"
exec "$@"
