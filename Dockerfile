# Builds ONE image that can run as the web server, the Celery worker or Celery Beat
# (the first word after the image name decides: web, worker, beat or all).
FROM python:3.12-slim

# No .pyc files, instant log output, smaller pip downloads.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) Install the packages FIRST. Docker caches this step until requirements.txt changes,
#    so rebuilding after a code change takes seconds instead of minutes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Copy the code (see .dockerignore: secrets and junk are NOT copied).
COPY . .

# 3) Collect the admin's CSS/JS. Django needs these two values just to START; they are fake,
#    exist only inside this one command, and are not stored in the image.
RUN SECRET_KEY=build-only-not-a-secret \
    DATABASE_URL=postgres://build:build@localhost:5432/build \
    python manage.py collectstatic --noinput

# 4) Windows editors may save the script with CRLF line endings, which Linux cannot run.
#    Fix that, make it executable, and run as a normal user instead of root.
RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
