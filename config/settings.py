"""NovaPay settings. SQLite + local-memory cache so the project runs with zero external services."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "payments",
    "ledger",
    "webhooks",
    "checkout",
    "dashboard",
    "demostore",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "core.middleware.RequestIdMiddleware",
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
        "DIRS": [BASE_DIR / "templates"],
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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        # IMMEDIATE: a transaction takes the write lock up-front, so concurrent payments/refunds queue
        # instead of failing with "database is locked" on lock upgrade.
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["core.auth.APIKeyAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "core.exceptions.gateway_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["core.throttles.MerchantRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"merchant": "120/min"},
}

# --- NovaPay domain settings -------------------------------------------------
FEE_PERCENT = 2          # gateway fee, % of amount
GST_PERCENT = 18         # GST charged on the fee
MIN_AMOUNT_PAISE = 100   # Rs 1
MAX_AMOUNT_PAISE = 50_000_000  # Rs 5,00,000
ORDER_EXPIRY_HOURS = 24
CARD_FINGERPRINT_SALT = os.environ.get("CARD_FINGERPRINT_SALT", "novapay-fp-salt")

# Webhook retry backoff in seconds: 10s, 1m, 5m, 30m, 2h  (spec: exponential-style backoff)
WEBHOOK_BACKOFF_SECONDS = [10, 60, 300, 1800, 7200]
WEBHOOK_TIMEOUT_SECONDS = 5
WEBHOOK_SIGNATURE_TOLERANCE = 300
# Background delivery thread (started automatically with `runserver`)
WEBHOOK_WORKER_ENABLED = "runserver" in sys.argv
WEBHOOK_WORKER_INTERVAL = 2

# Base URL the demo store uses to talk to the gateway API
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8000")

TESTING = "test" in sys.argv or "pytest" in sys.modules
if TESTING:
    WEBHOOK_WORKER_ENABLED = False
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
