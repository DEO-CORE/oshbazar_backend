#!/usr/bin/env bash
# Точка входа контейнера: миграции, статика, затем команда (gunicorn).
set -euo pipefail

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
