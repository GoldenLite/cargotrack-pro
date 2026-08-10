"""
Настройки проекта CargoTrack Pro.

Поведение управляется переменными окружения (см. .env.example).
Без env-переменных проект работает в dev-режиме как раньше.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# На Windows консольная кодировка по умолчанию cp1251 — логи с не-латиницей и
# стрелками ломали StreamHandler (UnicodeEncodeError). Переключаем stdout/stderr
# на utf-8 один раз при импорте настроек.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, 'reconfigure', None)
    if reconfigure is not None:
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == '':
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


def env_csv(name: str, default=None):
    v = os.environ.get(name, '')
    items = [x.strip() for x in v.split(',') if x.strip()]
    return items or (default or [])


ENV = os.environ.get('DJANGO_ENV', 'dev').strip().lower()
IS_PROD = ENV == 'prod'

# ── SECRET_KEY / DEBUG / ALLOWED_HOSTS ──────────────────────────────────────
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or (
    'dev-insecure-' + 'x' * 50
)

DEBUG = env_bool('DJANGO_DEBUG', not IS_PROD)

if IS_PROD:
    ALLOWED_HOSTS = env_csv('DJANGO_ALLOWED_HOSTS', ['127.0.0.1', 'localhost'])
else:
    ALLOWED_HOSTS = env_csv('DJANGO_ALLOWED_HOSTS', ['*'])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'cargo.apps.CargoConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'cargotrack.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'cargotrack.wsgi.application'

# ── База данных ──────────────────────────────────────────────────────────────
# По умолчанию SQLite (dev + текущий prod) — поведение как раньше.
# DJANGO_DB_ENGINE=postgres переключает DEFAULT на PostgreSQL (cutover миграции,
# см. postgres-migration). Алиас 'pg' поднимается когда заданы POSTGRES_* — он
# нужен на этапе КОПИРОВАНИЯ, пока default ещё SQLite, а данные льются в PG
# через .using('pg'). Без env-переменных ни одна строка ниже не меняет прод.
def _sqlite_db():
    return {
        'ENGINE': 'django.db.backends.sqlite3',
        # str, не Path: portable (Django сам делает str()), а backfill-снапшот
        # (shutil.copy) и VACUUM INTO ждут строковый путь.
        'NAME': str(BASE_DIR / 'db.sqlite3'),
        # SQLite concurrency настраивается через PRAGMA в cargo/apps.py
        # (connection_created signal: WAL + busy_timeout=60s). Это надёжнее
        # OPTIONS['timeout'] — PRAGMA повторяется на каждом новом соединении.
    }


def _postgres_db():
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'cargotrack'),
        'USER': os.environ.get('POSTGRES_USER', 'cargotrack'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        # Персистентные соединения: waitress-воркеры и кроны не переустанавливают
        # TCP на каждый запрос (SQLite такого не имел — там был file-open).
        'CONN_MAX_AGE': int(os.environ.get('DJANGO_CONN_MAX_AGE', '60') or 0),
        'OPTIONS': {'connect_timeout': 10},
    }


_db_engine = os.environ.get('DJANGO_DB_ENGINE', 'sqlite').strip().lower()
if _db_engine in ('postgres', 'postgresql', 'pg'):
    # Cutover: DEFAULT = PostgreSQL. SQLite остаётся под алиасом 'sqlite' для
    # read-only отката/сверки в первые дни после переключения.
    DATABASES = {'default': _postgres_db(), 'sqlite': _sqlite_db()}
else:
    # Обычный режим: DEFAULT = SQLite. Алиас 'pg' объявляем ТОЛЬКО если заданы
    # POSTGRES_* (этап копирования перед cutover) — иначе Django мог бы попытаться
    # достучаться до несуществующего PG. Нет env → ровно старый один-БД конфиг.
    DATABASES = {'default': _sqlite_db()}
    if os.environ.get('POSTGRES_HOST') or os.environ.get('POSTGRES_DB'):
        DATABASES['pg'] = _postgres_db()

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if IS_PROD
            else 'django.contrib.staticfiles.storage.StaticFilesStorage'
        ),
    },
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Email ───────────────────────────────────────────────────────────────────
if IS_PROD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'CargoTrack Pro <noreply@cargotrack.local>'
)

# ── Рассылка per-HAWB реестров ДТЭГ при выпуске (release-reestr-mailer) ───────
# Отдельный поток: на каждый выпущенный груз шлём реестр ДТЭГ на заданную почту,
# независимо от кривой службы «Регистрация» Альты. Выключено по умолчанию —
# включается REESTR_MAILER_ENABLED=True в .env.
REESTR_MAILER_ENABLED = env_bool('REESTR_MAILER_ENABLED', False)
REESTR_MAILER_TO = os.environ.get('REESTR_MAILER_TO', 'andylapshin@yandex.ru')
# Cutover (ISO-дата/время): шлём ТОЛЬКО по выпускам с release_date >= этой метки.
# Защита от «выстрела» по историческому бэклогу выпусков при первом включении.
# Пусто → шлём с любого выпуска (осторожно; на проде всегда задаём метку старта).
REESTR_MAILER_SINCE = os.environ.get('REESTR_MAILER_SINCE', '')
# Максимум писем за один прогон конвейера (защита от флуда/SMTP-лимитов).
REESTR_MAILER_CAP = int(os.environ.get('REESTR_MAILER_CAP', '25'))
# Сколько прогонов пытаться, если ещё нет решения-выпуска (CMN.11350 приходит
# ПОЗЖЕ статуса RELEASED) / нет подачи / упала отправка. При hourly-конвейере
# 48 ≈ 2 суток — с запасом на позднее прибытие отметки, потом «исчерпано».
REESTR_MAILER_MAX_ATTEMPTS = int(os.environ.get('REESTR_MAILER_MAX_ATTEMPTS', '48'))

# ── Рассылка регулярных ДТ (бланк-PDF) при выпуске (dt-mailer) ────────────────
# Отдельный поток: печатный бланк ДТ (его печатает Альта, агент кладёт PDF в
# C:\GTDSERV\ED\IN_PDF → залив на /api/v1/dt/pdf/) шлём на почту. Рег.№ ДТ — из
# имени файла GTD_<пост>_<ддммгг>_<номер>. Выключено по умолчанию.
DT_MAILER_ENABLED = env_bool('DT_MAILER_ENABLED', False)
DT_MAILER_TO = os.environ.get('DT_MAILER_TO', 'andylapshin@yandex.ru')
# Cutover (ISO-дата/время): шлём ТОЛЬКО по PDF, ПОЛУЧЕННЫМ (created_at) >= метки.
DT_MAILER_SINCE = os.environ.get('DT_MAILER_SINCE', '')
DT_MAILER_CAP = int(os.environ.get('DT_MAILER_CAP', '25'))
DT_MAILER_MAX_ATTEMPTS = int(os.environ.get('DT_MAILER_MAX_ATTEMPTS', '12'))
# Через сколько дней после отправки удалять PDF-файл с диска (аудит-строка
# остаётся). Транзитные файлы — в бэкап БД не попадают (лежат в MEDIA_ROOT).
DT_MAILER_PURGE_DAYS = int(os.environ.get('DT_MAILER_PURGE_DAYS', '14'))
# NB: DATA_UPLOAD_MAX_MEMORY_SIZE НЕ поднимаем глобально (это усилило бы
# unauth-DoS на публичный telegram-webhook, который парсит тело до auth).
# api_dt_pdf_post читает PDF ограниченным request.read(MAX+1), минуя этот лимит.

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'

# ── CORS ────────────────────────────────────────────────────────────────────
CORS_ALLOW_ALL_ORIGINS = not IS_PROD
CORS_ALLOWED_ORIGINS = env_csv('DJANGO_CORS_ALLOWED_ORIGINS')
CORS_ALLOW_CREDENTIALS = True

# ── CSRF ────────────────────────────────────────────────────────────────────
CSRF_TRUSTED_ORIGINS = env_csv('DJANGO_CSRF_TRUSTED_ORIGINS')

# ── СДЭК (CDEK) — read-only трекинг статусов доставки ─────────────────────────
# Заказ в СДЭК создаётся внешней системой с нашим hawb_number как im_number;
# статусы приходят вебхуками ORDER_STATUS на публичный endpoint
# /api/v1/cdek/webhook/<secret>/. См. cargo/services/cdek/.
CDEK_ENABLED            = env_bool('CDEK_ENABLED', False)
CDEK_API_BASE_URL       = (os.environ.get('CDEK_API_BASE_URL', '')
                           or 'https://api.cdek.ru').rstrip('/')
CDEK_CLIENT_ID          = os.environ.get('CDEK_CLIENT_ID', '')
CDEK_CLIENT_SECRET      = os.environ.get('CDEK_CLIENT_SECRET', '')
# Несекретный, но неугадываемый сегмент пути приёмника вебхуков.
CDEK_WEBHOOK_SECRET     = os.environ.get('CDEK_WEBHOOK_SECRET', '')
# Явный публичный https-URL приёмника (для регистрации подписки). Если пусто —
# cdek_register_webhook соберёт из хоста + secret.
CDEK_WEBHOOK_PUBLIC_URL = os.environ.get('CDEK_WEBHOOK_PUBLIC_URL', '')
# Опциональный allowlist source-IP вебхуков (CSV). Пусто = не проверять.
CDEK_WEBHOOK_ALLOWED_IPS = env_csv('CDEK_WEBHOOK_ALLOWED_IPS')
# Опц.: при терминальном DELIVERED от СДЭК продвигать logistics_status в
# DELIVERED (one-way, только из поздних статусов). По умолчанию выкл.
CDEK_AUTO_ADVANCE_DELIVERED = env_bool('CDEK_AUTO_ADVANCE_DELIVERED', False)

# ── «Декларант Плюс» — склад-API «Мой груз.ВХ» (Дальний Восток) ───────────────
# Внешний СВХ-источник для грузов идущих через склад «Таможенный портал»
# (Владивосток, ИНН 2536209470). Альта-СВХ обслуживает только Внуково,
# moscow-cargo — Шереметьево (префиксы 784/555/826/537/880); deklarant
# покрывает остальной ДВ-импорт. Доступ только через QR-сессию из БД
# (DeklarantSession); подробности — DEKLARANT_INTEGRATION_HANDOFF.md.
DEKLARANT_ENABLED         = env_bool('DEKLARANT_ENABLED', False)
DEKLARANT_MDT_BASE        = (os.environ.get('DEKLARANT_MDT_BASE', '')
                             or 'https://mdt.deklarant.ru').rstrip('/')
DEKLARANT_REGION          = os.environ.get('DEKLARANT_REGION', '107')        # ДВТУ
DEKLARANT_TARGET_WH_INN   = os.environ.get('DEKLARANT_TARGET_WH_INN', '2536209470')  # Таможенный портал
# SSL verify для requests. mdt.deklarant.ru:48774 (нестандартный порт)
# использует серт которого нет в certifi-bundle requests. urllib.request
# работает через системный truststore (Windows certstore / Linux ca-certs).
# Если включён verify=True и сертификат не валидируется — клиент падает.
# Probe-скрипт использует urllib и потому проблем не имел.
DEKLARANT_SSL_VERIFY      = env_bool('DEKLARANT_SSL_VERIFY', False)

# ── Удаление «ТО КЛИЕНТ» строк из CRM-вкладок специалистов ────────────────────
# Когда в Сводной ВЭД у HAWB в столбце «ФИО Специалист по ВЭД» стоит
# «ТО КЛИЕНТ» — таможенное оформление делает клиент сам. Команда
# delete_to_client_hawbs физически удаляет такие строки из CRM-вкладок
# («Рабочее пространство СТО»). В «Общем» HAWB остаётся (по дизайну).
# Snapshot перед удалением в backups/to_client_snapshots/.
DELETE_TO_CLIENT_ENABLED  = env_bool('DELETE_TO_CLIENT_ENABLED', False)

# ── Django REST Framework ───────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

# ── Логирование ─────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'cargotrack.log',
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'cargo': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
    },
}

# ── Production-only security ────────────────────────────────────────────────
if IS_PROD:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '0') or 0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    if env_bool('DJANGO_SECURE_PROXY_SSL_HEADER'):
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    if not os.environ.get('DJANGO_SECRET_KEY'):
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured('DJANGO_SECRET_KEY обязателен в prod')


# ── Локальные настройки (НЕ в git): TELEGRAM_ALERT, DEKLARANT_ALERT_TELEGRAM,
# и другие per-deploy секреты. Файл cargotrack/local_settings.py создаётся
# вручную на VPS, в .gitignore.
try:
    from .local_settings import *  # noqa: F401,F403
except ImportError:
    pass
