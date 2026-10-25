#!/bin/bash
# One script, four jobs. Usage:  entrypoint.sh web | worker | beat | all
#   web     migrate the database, then start Gunicorn (the web server)
#   worker  start the Celery worker (runs background tasks)
#   beat    start Celery Beat (sends the scheduled tasks)
#   all     web + worker + beat inside ONE container (for small or free hosting)
set -euo pipefail

MODE="${1:-web}"
PORT="${PORT:-8000}"

migrate() {
  # Retry, because on a fresh start the database may need a few seconds to become ready.
  echo "[entrypoint] applying database migrations..."
  local attempts="${MIGRATE_ATTEMPTS:-30}"
  local wait_seconds="${MIGRATE_WAIT:-3}"
  for attempt in $(seq 1 "$attempts"); do
    if python manage.py migrate --noinput; then
      return 0
    fi
    echo "[entrypoint] database not ready (attempt ${attempt}/${attempts}), retrying in ${wait_seconds} seconds..."
    sleep "$wait_seconds"
  done
  echo "[entrypoint] giving up: the database never became ready" >&2
  exit 1
}

case "$MODE" in
  web)
    if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
      migrate
    fi
    exec gunicorn config.wsgi:application \
      --bind "0.0.0.0:${PORT}" \
      --workers "${WEB_CONCURRENCY:-3}" \
      --timeout 30 \
      --access-logfile - \
      --error-logfile -
    ;;

  worker)
    exec celery -A config worker --loglevel=info --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;

  beat)
    # /tmp is writable even when the rest of the container is not.
    exec celery -A config beat --loglevel=info --schedule /tmp/celerybeat-schedule
    ;;

  all)
    migrate
    celery -A config worker --beat --loglevel=info --concurrency=1 \
      --schedule /tmp/celerybeat-schedule &
    CELERY_PID=$!
    # Started directly (no helper function), so $! really is Gunicorn's own process id and
    # "kill" reaches it. A function would hide it inside a sub-shell and it would keep running.
    gunicorn config.wsgi:application \
      --bind "0.0.0.0:${PORT}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --timeout 30 \
      --access-logfile - \
      --error-logfile - &
    GUNICORN_PID=$!

    stop_everything() {
      kill -TERM "$CELERY_PID" "$GUNICORN_PID" 2>/dev/null || true
      wait 2>/dev/null || true
    }
    trap stop_everything TERM INT

    # If EITHER process dies, stop the other one and exit, so the platform restarts the container.
    set +e
    wait -n
    STATUS=$?
    stop_everything
    exit "$STATUS"
    ;;

  *)
    exec "$@"
    ;;
esac
