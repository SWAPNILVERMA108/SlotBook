"""SlotBook settings: the FINAL version (Day 1 to Day 13 in one file).

Everything that changes between your laptop and a real server is read from environment
variables (or from your .env file). The code stays the same everywhere.
"""

import sys
from datetime import timedelta
from pathlib import Path

import dj_database_url
from decouple import Csv, config

# The folder that contains manage.py
BASE_DIR = Path(__file__).resolve().parent.parent

# True while running the automatic tests (pytest or manage.py test).
TESTING = "pytest" in sys.modules or (len(sys.argv) > 1 and sys.argv[1] == "test")

# ---------- Security (values come from the environment / .env file) ----------
SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
# Needed for the admin login over https behind a proxy, e.g. https://my-app.onrender.com
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# Render supplies these values for web services. Keep explicit environment values
# and add the Render values only when they are available.
_render_hostname = config("RENDER_EXTERNAL_HOSTNAME", default="").strip()
if _render_hostname and _render_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_hostname)

_render_url = config("RENDER_EXTERNAL_URL", default="").strip().rstrip("/")
if _render_url and _render_url not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_render_url)

# ---------- CORS: allow the local React frontend to call this API ----------
_DEFAULT_FRONTENDS = "http://localhost:5173,http://127.0.0.1:5173" if DEBUG else ""
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default=_DEFAULT_FRONTENDS, cast=Csv())
CORS_EXPOSE_HEADERS = ["Retry-After", "Date"]

# ---------- Apps ----------
INSTALLED_APPS = [
    # Django's built-in apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party apps
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    # Our own apps
    "users",
    "venues",
    "bookings",
    "payments",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves the admin's CSS/JS from the app itself (no separate web server needed).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------- Database: PostgreSQL ----------
# Hosting platforms give ONE address, DATABASE_URL (postgres://user:password@host:5432/name).
# On your laptop you can keep using the separate DB_* values from your .env file.
DATABASE_URL = config("DATABASE_URL", default="")
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(DATABASE_URL, conn_max_age=60, conn_health_checks=True)
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": config("DB_NAME"),
            "USER": config("DB_USER"),
            "PASSWORD": config("DB_PASSWORD"),
            "HOST": config("DB_HOST", default="localhost"),
            "PORT": config("DB_PORT", default="5432"),
        }
    }
# Give up after 5 seconds if the database does not answer (the health check must never hang).
DATABASES["default"].setdefault("OPTIONS", {})["connect_timeout"] = 5

# ---------- Users ----------
# We use our own User model. Must be set before the first migrate!
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
if TESTING:
    # A fast (and insecure) hasher. Safe here because it is only used by the tests.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# ---------- Language and time ----------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True  # store times in UTC inside the database (very important for bookings)

# ---------- Static files ----------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # `collectstatic` copies the admin files here
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------- Cache ----------
# The cache stores the rate-limit counters. With several server processes, use Redis
# (set CACHE_URL) so they all count together. Without CACHE_URL each process counts alone.
CACHE_URL = config("CACHE_URL", default="")
if CACHE_URL:
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": CACHE_URL}
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# ---------- Rate limiting (Day 13) ----------
THROTTLING_ENABLED = config("THROTTLING_ENABLED", default=True, cast=bool) and not TESTING
THROTTLE_RATES = {
    "anon": config("THROTTLE_RATE_ANON", default="60/min"),  # not logged in, per IP address
    "user": config("THROTTLE_RATE_USER", default="300/min"),  # logged in, per user
    "auth": config("THROTTLE_RATE_AUTH", default="10/min"),  # register / login / refresh, per IP
}

# ---------- Django REST Framework ----------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # Every API needs a logged-in user unless we say otherwise
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "config.throttling.AnonThrottle",
        "config.throttling.UserThrottle",
    ),
    # How many proxies sit in front of the app. 0 = trust only the direct connection's address.
    # (Render / Railway: set NUM_PROXIES=1, otherwise every visitor looks like the same address.)
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SlotBook API",
    "DESCRIPTION": "Slot booking backend with safe concurrent booking and payments.",
    "VERSION": "1.0.0",
}

# ---------- Booking rules ----------
BOOKING_HOLD_MINUTES = 10  # how long a PENDING booking holds a slot

# ---------- Celery (background tasks) ----------
# REDIS_URL (hosting platforms) or CELERY_BROKER_URL (your laptop) tells Celery where Redis is.
REDIS_URL = config("REDIS_URL", default="")
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL or "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL  # task results are stored in Redis too
CELERY_ACCEPT_CONTENT = ["json"]  # only accept JSON messages (safer than pickle)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60  # a task running longer than 60 seconds is killed
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # keep retrying if Redis is not ready yet

# DEVELOPMENT SHORTCUT: add  CELERY_EAGER=True  to your .env and every task runs immediately
# inside Django (no Redis, no worker). NEVER use it in production.
# (The settings only exist when switched on, so the automatic tests can toggle eager mode.)
if config("CELERY_EAGER", default=False, cast=bool):
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

# Tasks that run on a schedule (needs `celery -A config beat`)
CELERY_BEAT_SCHEDULE = {
    # Day 9: free the slots whose 10-minute hold has ended.
    "release-expired-bookings": {
        "task": "bookings.tasks.release_expired_bookings",
        "schedule": 60.0,  # seconds
    },
    # Day 11: safety net that retries refunds that were not sent yet.
    "process-pending-refunds": {
        "task": "payments.tasks.process_pending_refunds",
        "schedule": 300.0,
    },
}

# ---------- Razorpay (use TEST keys from your Razorpay dashboard) ----------
# Never write real keys in this file. They come from .env, which Git ignores.
RAZORPAY_KEY_ID = config("RAZORPAY_KEY_ID", default="")
RAZORPAY_KEY_SECRET = config("RAZORPAY_KEY_SECRET", default="")
RAZORPAY_WEBHOOK_SECRET = config("RAZORPAY_WEBHOOK_SECRET", default="")

# ---------- Logging (so we can SEE what happens) ----------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "bookings": {"handlers": ["console"], "level": "INFO"},
        "payments": {"handlers": ["console"], "level": "INFO"},
        "config": {"handlers": ["console"], "level": "INFO"},
    },
}

# ---------- Production security (Day 13) ----------
# All of this is OFF on your laptop and switched on with environment variables on a server:
#   HTTPS_ONLY=True    -> https redirect, secure cookies, HSTS
#   BEHIND_PROXY=True  -> trust the proxy's "X-Forwarded-Proto" header (Render, Railway, nginx)
BEHIND_PROXY = config("BEHIND_PROXY", default=False, cast=bool)
HTTPS_ONLY = config("HTTPS_ONLY", default=False, cast=bool)

if BEHIND_PROXY:
    # Only turn this on when a proxy really sets (and overwrites) this header,
    # otherwise visitors could fake "https".
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if HTTPS_ONLY and not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]  # platform health checks often use plain http
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS tells browsers "always use https for this site". Start small (1 hour) and raise
    # it later, because browsers remember it and a mistake is hard to undo.
    SECURE_HSTS_SECONDS = config("HSTS_SECONDS", default=3600, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = config("HSTS_INCLUDE_SUBDOMAINS", default=False, cast=bool)
    SECURE_HSTS_PRELOAD = False
    # "Preload" puts your site in the browsers' built-in https list: a one-way, long-term
    # promise. We leave it off on purpose, so we silence Django's reminder about it.
    SILENCED_SYSTEM_CHECKS = ["security.W021"]
