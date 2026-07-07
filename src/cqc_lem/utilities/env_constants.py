import os

MISSING_CONSTANTS = []


def isTrue(s: str) -> bool:
    return s.lower() in ['true', '1', 't', 'y', 'yes']


def get_constant_from_env(key: str, required: bool = False, default_value: str = None) -> str:
    if required:
        return os.environ[key]
    else:
        const = os.environ.get(key)
        if not const:
            MISSING_CONSTANTS.append(key)
            return default_value
        else:
            return const


# Get constants from GitHub Actions
try:
    IS_GITHUB_ACTION = isTrue(get_constant_from_env('GITHUB_ACTION_TRUE', default_value='False'))
except KeyError:
    IS_GITHUB_ACTION = False

# Get constants from environment .env file
OPENAI_API_KEY = get_constant_from_env('OPENAI_API_KEY')
LI_USER = get_constant_from_env('LI_USER')
LI_PASSWORD = get_constant_from_env('LI_PASSWORD')
LI_CLIENT_ID = get_constant_from_env('LI_CLIENT_ID')
LI_CLIENT_SECRET = get_constant_from_env('LI_CLIENT_SECRET')
LI_REDIRECT_URL = get_constant_from_env('LI_REDIRECT_URL')
LI_STATE_SALT = get_constant_from_env('LI_STATE_SALT')
LI_API_VERSION = get_constant_from_env('LI_API_VERSION', default_value='202501')
PEXELS_API_KEY = get_constant_from_env('PEXELS_API_KEY')
PERPLEXITY_API_KEY = get_constant_from_env('PERPLEXITY_API_KEY')
HF_TOKEN = get_constant_from_env('HF_TOKEN')
REPLICATE_API_TOKEN = get_constant_from_env('REPLICATE_API_TOKEN')
REPLICATE_USERNAME = get_constant_from_env('REPLICATE_USERNAME', default_value='')
RUNWAYML_API_SECRET = get_constant_from_env('RUNWAYML_API_SECRET')

# --- Media generation defaults ---
# Video model: gen4_turbo (default, cheap, drop-in for the sunsetting gen3a_turbo),
# gen4.5 (quality) and veo3.1 (realism) are opt-in per-call. Ratio is the Runway
# resolution string; 960:960 is square 1:1 to match the base image.
DEFAULT_VIDEO_MODEL = get_constant_from_env('DEFAULT_VIDEO_MODEL', default_value='gen4_turbo')
DEFAULT_VIDEO_RATIO = get_constant_from_env('DEFAULT_VIDEO_RATIO', default_value='960:960')
DEFAULT_IMAGE_MODEL = get_constant_from_env('DEFAULT_IMAGE_MODEL', default_value='black-forest-labs/flux-dev')
DEFAULT_IMAGE_RATIO = get_constant_from_env('DEFAULT_IMAGE_RATIO', default_value='1:1')

# Video quality tiers. STANDARD is free (included in every plan); PREMIUM tiers cost
# video credits (deducted on a successful premium render, refunded on failure).
# Premium uses Veo (realism + native audio); with an active avatar it runs Veo
# image->video on the avatar image to keep the user's likeness, else text->video.
STANDARD_VIDEO_MODEL = get_constant_from_env('STANDARD_VIDEO_MODEL', default_value='gen4_turbo')
PREMIUM_VIDEO_MODEL = get_constant_from_env('PREMIUM_VIDEO_MODEL', default_value='veo3.1_fast')   # 1 credit
PREMIUM_TOP_VIDEO_MODEL = get_constant_from_env('PREMIUM_TOP_VIDEO_MODEL', default_value='veo3.1')  # 3 credits
PREMIUM_VIDEO_CREDITS = int(get_constant_from_env('PREMIUM_VIDEO_CREDITS', default_value='1'))
PREMIUM_TOP_VIDEO_CREDITS = int(get_constant_from_env('PREMIUM_TOP_VIDEO_CREDITS', default_value='3'))

# --- Carousel content-slide imagery ---
# Content (middle) carousel slides carry a relevant image so they don't render as
# text-on-blank. Sourcing is deterministic and cheap-first: Pexels stock (just an
# API call) is the default source; Replicate generation is OFF by default (costs
# money + only adds value with the user's active avatar). Rates are 0..1 fractions
# applied against a per-(post_id, slide) deterministic seed so regeneration is stable.
CAROUSEL_IMAGES_ENABLED = isTrue(get_constant_from_env('CAROUSEL_IMAGES_ENABLED', default_value='True'))
CAROUSEL_PEXELS_ENABLED = isTrue(get_constant_from_env('CAROUSEL_PEXELS_ENABLED', default_value='True'))
CAROUSEL_REPLICATE_ENABLED = isTrue(get_constant_from_env('CAROUSEL_REPLICATE_ENABLED', default_value='False'))
# Fraction of eligible content slides that MAY use Replicate generation (only when
# CAROUSEL_REPLICATE_ENABLED and the user has an active, relevant avatar). Low by default.
CAROUSEL_REPLICATE_RATE = float(get_constant_from_env('CAROUSEL_REPLICATE_RATE', default_value='0.34'))
# Fraction of content slides that get an image at all (1.0 = every content slide).
CAROUSEL_IMAGE_RATE = float(get_constant_from_env('CAROUSEL_IMAGE_RATE', default_value='1.0'))
# Use an LLM (lem-simple) to derive visual keywords from slide meaning. When off,
# a local keyword-extraction heuristic is used (no API call).
CAROUSEL_IMAGE_QUERY_LLM = isTrue(get_constant_from_env('CAROUSEL_IMAGE_QUERY_LLM', default_value='True'))

# AI disclosure: append a short caption line to AI-visual posts. Guaranteed-visible
# fallback for C2PA (which self-signed certs can't make LinkedIn trust yet).
AI_DISCLOSURE_ENABLED = isTrue(get_constant_from_env('AI_DISCLOSURE_ENABLED', default_value='True'))
AI_DISCLOSURE_TEXT = get_constant_from_env('AI_DISCLOSURE_TEXT', default_value='\n\n(Visuals created with AI)')

# C2PA Content Credentials: best-effort signing of generated assets. No-ops unless
# enabled AND a cert+key pair is present. Self-signed is valid C2PA but flagged
# untrusted by validators — swap in a CA-issued cert (no code change) to gain trust.
C2PA_ENABLED = isTrue(get_constant_from_env('C2PA_ENABLED', default_value='False'))
C2PA_CERT_PATH = get_constant_from_env('C2PA_CERT_PATH', default_value='')
C2PA_KEY_PATH = get_constant_from_env('C2PA_KEY_PATH', default_value='')

TZ = get_constant_from_env('TZ', default_value='UTC')
CQC_LEM_CHECK_SCHEDULE_DELTA_MINUTES = int(get_constant_from_env('CQC_LEM_CHECK_SCHEDULE_DELTA_MINUTES', default_value='5'))
CQC_LEM_POST_TIME_DELTA_MINUTES = int(get_constant_from_env('CQC_LEM_POST_TIME_DELTA_MINUTES', default_value='20'))
API_PORT=get_constant_from_env('API_PORT', default_value='8000')
DEVICE_FARM_PROJECT_ARN=get_constant_from_env('DEVICE_FARM_PROJECT_ARN')
TEST_GRID_PROJECT_ARN=get_constant_from_env('TEST_GRID_PROJECT_ARN')
SELENIUM_HUB_HOST=get_constant_from_env('SELENIUM_HUB_HOST', default_value='selenium-chrome')
SELENIUM_HUB_PORT=get_constant_from_env('SELENIUM_HUB_PORT', default_value='4444')
SELENIUM_REGISTRATION_SECRET=get_constant_from_env('SELENIUM_REGISTRATION_SECRET', default_value='secret')
SELENIUM_KEEP_VIDEOS_X_DAYS=int(get_constant_from_env('SELENIUM_KEEP_VIDEOS_X_DAYS', default_value='7'))
SELENIUM_RECORD_VIDEOS=isTrue(get_constant_from_env('SELENIUM_RECORD_VIDEOS', default_value='False'))
STREAMLIT_EMAIL=get_constant_from_env('STREAMLIT_EMAIL')
HEADLESS_BROWSER = isTrue(get_constant_from_env('HEADLESS_BROWSER', default_value='True'))
CODE_TRACING = isTrue(get_constant_from_env('CODE_TRACING', default_value='False'))
WAIT_DEFAULT_TIMEOUT = float(get_constant_from_env('WAIT_DEFAULT_TIMEOUT', default_value='15'))
MAX_WAIT_RETRY = int(get_constant_from_env('MAX_WAIT_RETRY', default_value='2'))
RETRY_PARSER_MAX_RETRY = int(get_constant_from_env('RETRY_PARSER_MAX_RETRY', default_value='3'))
SHOW_ERROR_LINE_NUMBERS = isTrue(get_constant_from_env('SHOW_ERROR_LINE_NUMBERS', default_value='False'))
PURGE_TASKS = isTrue(get_constant_from_env('PURGE_TASKS', default_value='False'))
CLEAR_SELENIUM_SESSIONS = isTrue(get_constant_from_env('CLEAR_SELENIUM_SESSIONS', default_value='False'))

AWS_APPLICATION_NAME=get_constant_from_env('AWS_APPLICATION_NAME', default_value='CQC=LEM')
AWS_APPLICATION_TAG=get_constant_from_env('AWS_APPLICATION_TAG', default_value='CQC=LEM')
AWS_ENV_TAG=get_constant_from_env('AWS_ENV_TAG', default_value='dev')
AWS_REGION=get_constant_from_env('AWS_REGION')
AWS_MYSQL_SECRET_NAME=get_constant_from_env('AWS_MYSQL_SECRET_NAME')

NGROK_CUSTOM_DOMAIN=get_constant_from_env('NGROK_CUSTOM_DOMAIN')
NGROK_FREE_DOMAIN=get_constant_from_env('NGROK_FREE_DOMAIN')
NGROK_API_PREFIX=get_constant_from_env('NGROK_API_PREFIX')

# Production public base URL — full scheme+host, no port, no trailing slash
# (e.g. https://lem.example.com). Canonical prod setting; takes precedence over
# all ngrok-derived URLs (use this when NGROK_PLAN=off).
PUBLIC_BASE_URL=get_constant_from_env('PUBLIC_BASE_URL')
if PUBLIC_BASE_URL:
    PUBLIC_BASE_URL = PUBLIC_BASE_URL.rstrip('/')

# Dynamic LinkedIn redirect URL precedence:
#   PUBLIC_BASE_URL (prod) > NGROK_CUSTOM_DOMAIN (dev/ngrok) > static LI_REDIRECT_URL env.
if PUBLIC_BASE_URL:
    LI_REDIRECT_URL = f"{PUBLIC_BASE_URL}/auth/linkedin/callback"
elif NGROK_CUSTOM_DOMAIN:
    LI_REDIRECT_URL = f"https://{NGROK_CUSTOM_DOMAIN}/auth/linkedin/callback"

# API base URL precedence:
#   PUBLIC_BASE_URL (prod, port-less) > NGROK_CUSTOM_DOMAIN > PREFIX.FREE_DOMAIN
#   > API_BASE_URL env with :PORT (local dev fallback).
if PUBLIC_BASE_URL:
    API_BASE_URL = PUBLIC_BASE_URL
    API_URL_FINAL = PUBLIC_BASE_URL
elif NGROK_CUSTOM_DOMAIN:
    API_BASE_URL = f"https://{NGROK_CUSTOM_DOMAIN}"
    API_URL_FINAL = API_BASE_URL
elif NGROK_FREE_DOMAIN and NGROK_API_PREFIX:
    API_BASE_URL = f"https://{NGROK_API_PREFIX}.{NGROK_FREE_DOMAIN}"
    API_URL_FINAL = API_BASE_URL
else:
    API_BASE_URL = get_constant_from_env('API_BASE_URL', default_value='http://localhost')
    API_URL_FINAL = f"{API_BASE_URL}:{API_PORT}"

SENDGRID_API_KEY     = get_constant_from_env('SENDGRID_API_KEY')
SENDGRID_FROM_EMAIL  = get_constant_from_env('SENDGRID_FROM_EMAIL', default_value='noreply@example.com')
# Friendly From display name and Reply-To for outbound transactional mail. A recognizable
# From name that matches the sending domain and a real Reply-To reduce spam/phishing scores.
EMAIL_FROM_NAME      = get_constant_from_env('EMAIL_FROM_NAME', default_value='Christopher Queen Consulting')
EMAIL_REPLY_TO       = get_constant_from_env('EMAIL_REPLY_TO', default_value='')

# SMTP fallback (Gmail or any STARTTLS-capable server)
SMTP_HOST     = get_constant_from_env('SMTP_HOST', default_value='smtp.gmail.com')
SMTP_PORT     = int(get_constant_from_env('SMTP_PORT', default_value='587'))
SMTP_USER     = get_constant_from_env('SMTP_USER')
SMTP_PASSWORD = get_constant_from_env('SMTP_PASSWORD')

# Persistent-login session lifetime. Sliding idle window (session stays alive as long as the
# user is active within SESSION_IDLE_HOURS) capped by an absolute maximum from first login.
SESSION_IDLE_HOURS        = int(get_constant_from_env('SESSION_IDLE_HOURS', default_value='24'))
SESSION_ABSOLUTE_MAX_DAYS = int(get_constant_from_env('SESSION_ABSOLUTE_MAX_DAYS', default_value='30'))

STRIPE_API_KEY            = get_constant_from_env('STRIPE_API_KEY')
STRIPE_WEBHOOK_SECRET     = get_constant_from_env('STRIPE_WEBHOOK_SECRET')
STRIPE_PRICE_ID_STARTER       = get_constant_from_env('STRIPE_PRICE_ID_STARTER')
STRIPE_PRICE_ID_PROFESSIONAL  = get_constant_from_env('STRIPE_PRICE_ID_PROFESSIONAL')
STRIPE_PRICE_ID_ENTERPRISE    = get_constant_from_env('STRIPE_PRICE_ID_ENTERPRISE')
FREE_TRIAL_DAYS           = int(get_constant_from_env('FREE_TRIAL_DAYS', default_value='14'))

NGROK_LIPREVIEW_PREFIX=get_constant_from_env('NGROK_LIPREVIEW_PREFIX')
if NGROK_FREE_DOMAIN and NGROK_LIPREVIEW_PREFIX:
    LINKEDIN_PREVIEW_URL = f"https://{NGROK_LIPREVIEW_PREFIX}.{NGROK_FREE_DOMAIN}"
else:
    LINKEDIN_PREVIEW_URL = get_constant_from_env('LINKEDIN_PREVIEW_URL', default_value='http://localhost:8081')

ADMIN_SECRET = get_constant_from_env('ADMIN_SECRET')

# Comma-separated bearer tokens accepted on /api routes. Empty disables the gate
# (local/dev) so only deployments that set it enforce token auth.
API_ACCESS_TOKENS = get_constant_from_env('API_ACCESS_TOKENS', default_value='')

# Set other constants here
USE_DOCKER_BROWSER = isTrue(get_constant_from_env('USE_DOCKER_BROWSER', default_value='True'))

# SPA footer version display. SHOW_VERSION_FOOTER toggles the small release-version
# text in the copyright footer (default on; set to false to hide it). APP_VERSION
# overrides the auto-detected version; when empty it falls back to the installed
# package metadata, which reflects the exact release baked into the running image.
SHOW_VERSION_FOOTER = isTrue(get_constant_from_env('SHOW_VERSION_FOOTER', default_value='True'))
APP_VERSION = get_constant_from_env('APP_VERSION', default_value='')


def get_app_version() -> str:
    if APP_VERSION:
        return APP_VERSION
    try:
        from importlib.metadata import version
        return version('linkedin-engagement-manager')
    except Exception:
        return 'unknown'
