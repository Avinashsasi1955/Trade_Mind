import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding the process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


_load_env(ROOT / ".env")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_PATH = Path(os.getenv("NIVESH_DATABASE", DATA_DIR / "nivesh.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
REDIS_URL = os.getenv("REDIS_URL", "")
DATABASE_POOL_MIN = int(os.getenv("DATABASE_POOL_MIN", "1"))
DATABASE_POOL_MAX = int(os.getenv("DATABASE_POOL_MAX", "12"))
JWT_SECRET = os.getenv("NIVESH_SECRET", "change-this-secret-before-deployment")
MODEL_ARTIFACT_KEY = os.getenv("NIVESH_MODEL_ARTIFACT_KEY", JWT_SECRET)
BROKER_SESSION_KEY = os.getenv("NIVESH_BROKER_SESSION_KEY", JWT_SECRET)
TOKEN_TTL_SECONDS = int(os.getenv("NIVESH_TOKEN_TTL", "86400"))
COOKIE_SECURE = os.getenv("NIVESH_COOKIE_SECURE", "0") == "1"
APP_ENV = os.getenv("NIVESH_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
DEMO_MODE = os.getenv("NIVESH_DEMO_MODE", "1" if not IS_PRODUCTION else "0") == "1"
SIGNUP_ENABLED = os.getenv("NIVESH_SIGNUP_ENABLED", "1" if not IS_PRODUCTION else "0") == "1"
ADMIN_EMAILS = {value.strip().lower() for value in os.getenv("NIVESH_ADMIN_EMAILS", "").split(",") if value.strip()}
ALLOWED_ORIGINS = {value.strip().rstrip("/") for value in os.getenv("NIVESH_ALLOWED_ORIGINS", "").split(",") if value.strip()}
COGNITO_AUTH_REQUIRED = os.getenv("NIVESH_COGNITO_AUTH_REQUIRED", "0") == "1"
TRUSTED_ALB_ARN = os.getenv("NIVESH_TRUSTED_ALB_ARN", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
COGNITO_LOGOUT_URL = os.getenv("NIVESH_COGNITO_LOGOUT_URL", "")
DEFAULT_CAPITAL = 1_000_000.0
HOST = os.getenv("NIVESH_HOST", "127.0.0.1")
PORT = int(os.getenv("NIVESH_PORT", "4173"))
LLM_BASE_URL = os.getenv("NIVESH_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("NIVESH_LLM_API_KEY", "")
LLM_MODEL = os.getenv("NIVESH_LLM_MODEL", "")
MODEL_PROVIDER = os.getenv("NIVESH_MODEL_PROVIDER", "auto").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "0") == "1"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
AI_GATEWAY_RATE_LIMIT = int(os.getenv("AI_GATEWAY_RATE_LIMIT", "10"))
AI_GATEWAY_CACHE_SECONDS = int(os.getenv("AI_GATEWAY_CACHE_SECONDS", "600"))
AI_GATEWAY_CIRCUIT_SECONDS = int(os.getenv("AI_GATEWAY_CIRCUIT_SECONDS", "120"))
AI_RAG_ENABLED = os.getenv("NIVESH_AI_RAG_ENABLED", "1") == "1"
AI_RAG_TOP_K = int(os.getenv("NIVESH_AI_RAG_TOP_K", "6"))
AI_RAG_MULTI_MODEL_ENABLED = os.getenv("NIVESH_AI_RAG_MULTI_MODEL_ENABLED", "1") == "1"
AI_RAG_MAX_MODELS = int(os.getenv("NIVESH_AI_RAG_MAX_MODELS", "3"))
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL", f"http://{HOST}:{PORT}/api/broker/zerodha/callback")
MARKET_DATA_PROVIDER = os.getenv("NIVESH_MARKET_DATA_PROVIDER", "zerodha").strip().lower()
BROKER_ROUTING = os.getenv("NIVESH_BROKER_ROUTING", "auto").strip().lower()
UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", os.getenv("UPSTOX_ANALYTICS_TOKEN", ""))
UPSTOX_ORDER_ACCESS_TOKEN = os.getenv("UPSTOX_ORDER_ACCESS_TOKEN", "").strip()
NIVESH_SHADOW_FORCE_FLAT_IST = os.getenv("NIVESH_SHADOW_FORCE_FLAT_IST", "15:15")
PRICE_MAX_AGE_SECONDS = int(os.getenv("NIVESH_PRICE_MAX_AGE_SECONDS", "60"))
PRICE_MAX_AGE_SWING_SECONDS = int(os.getenv("NIVESH_PRICE_MAX_AGE_SWING_SECONDS", "900"))
STALE_DATA_EXIT_MINUTES = int(os.getenv("NIVESH_STALE_DATA_EXIT_MINUTES", "5"))
HELD_CONTRACT_STALE_TICK_MINUTES = int(os.getenv("NIVESH_HELD_CONTRACT_STALE_TICK_MINUTES", "3"))
STALE_OPEN_GRACE_SECONDS = int(os.getenv("NIVESH_STALE_OPEN_GRACE_SECONDS", "120"))
SWING_MAX_STOP_ATR = float(os.getenv("NIVESH_SWING_MAX_STOP_ATR", "3.0"))
SWING_MAX_POSITION_PCT = float(os.getenv("NIVESH_SWING_MAX_POSITION_PCT", "0.20"))
PROFIT_HARVEST_ENABLED = os.getenv("NIVESH_PROFIT_HARVEST_ENABLED", "0") == "1"
HARVEST_TRIGGER = float(os.getenv("NIVESH_HARVEST_TRIGGER", "0.83"))
HARVEST_EXIT_THRESHOLD = float(os.getenv("NIVESH_HARVEST_EXIT_THRESHOLD", "70.0"))
HARVEST_TIGHTEN_THRESHOLD = float(os.getenv("NIVESH_HARVEST_TIGHTEN_THRESHOLD", "40.0"))
HARVEST_LOCK_FRACTION = float(os.getenv("NIVESH_HARVEST_LOCK_FRACTION", "0.65"))
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", f"http://{HOST}:{PORT}/api/broker/upstox/callback")
UPSTOX_AUTHORIZE_URL = os.getenv("UPSTOX_AUTHORIZE_URL", "https://api.upstox.com/v3/feed/market-data-feed/authorize")
UPSTOX_INSTRUMENTS_URL = os.getenv("UPSTOX_INSTRUMENTS_URL", "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz")
LIVE_TRADING_ENABLED = os.getenv("NIVESH_LIVE_TRADING_ENABLED", "0") == "1"
LIVE_ELIGIBLE = os.getenv("LIVE_ELIGIBLE", "FALSE").upper() == "TRUE"
MARKET_HISTORY_PATH = Path(os.getenv("NIVESH_MARKET_HISTORY", DATA_DIR / "market_history.db"))
ML_RESEARCH_PATH = Path(os.getenv("NIVESH_ML_RESEARCH", DATA_DIR / "ml_research.db"))
ML_MIN_TRAIN_SAMPLES = int(os.getenv("NIVESH_ML_MIN_TRAIN_SAMPLES", "120"))
ML_RETRAIN_DAYS = int(os.getenv("NIVESH_ML_RETRAIN_DAYS", "7"))
ML_AUTO_RETRAIN = os.getenv("NIVESH_ML_AUTO_RETRAIN", "1") == "1"
ML_MAX_SYMBOLS_PER_SESSION = int(os.getenv("NIVESH_ML_MAX_SYMBOLS_PER_SESSION", "500"))
ML_MIN_ADTV = float(os.getenv("NIVESH_ML_MIN_ADTV", "10000000"))
ML_MIN_COVERAGE_PCT = float(os.getenv("NIVESH_ML_MIN_COVERAGE_PCT", "95"))
ML_MIN_DAILY_BARS = int(os.getenv("NIVESH_ML_MIN_DAILY_BARS", "250"))
ML_TRAINING_EXCHANGES = {value.strip().upper() for value in os.getenv("NIVESH_ML_TRAINING_EXCHANGES", "NSE").split(",") if value.strip()}
BACKUP_DIR = Path(os.getenv("NIVESH_BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_RETENTION_DAYS = int(os.getenv("NIVESH_BACKUP_RETENTION_DAYS", "14"))
NEWS_PROVIDER = os.getenv("NIVESH_NEWS_PROVIDER", "newsapi").lower()
NEWS_API_BASE_URL = os.getenv("NIVESH_NEWS_API_BASE_URL", "https://newsapi.org/v2/everything").rstrip("/")
NEWS_API_KEY = os.getenv("NIVESH_NEWS_API_KEY", "")
NEWS_API_KEY_HEADER = os.getenv("NIVESH_NEWS_API_KEY_HEADER", "X-Api-Key")
NEWS_CACHE_SECONDS = int(os.getenv("NIVESH_NEWS_CACHE_SECONDS", "900"))
NEWS_MAX_ARTICLES = int(os.getenv("NIVESH_NEWS_MAX_ARTICLES", "30"))
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", NEWS_API_KEY)
FINNHUB_WEBHOOK_URL = os.getenv("FINNHUB_WEBHOOK_URL", "")
FINNHUB_WEBHOOK_SECRET = os.getenv("FINNHUB_WEBHOOK_SECRET", "")
FINNHUB_NEWS_LOOKBACK_DAYS = int(os.getenv("FINNHUB_NEWS_LOOKBACK_DAYS", "7"))
FINNHUB_NEWS_CATEGORY = os.getenv("FINNHUB_NEWS_CATEGORY", "general")
SENTIMENT_MODEL_URL = os.getenv("NIVESH_SENTIMENT_MODEL_URL", "").rstrip("/")
SENTIMENT_MODEL_API_KEY = os.getenv("NIVESH_SENTIMENT_MODEL_API_KEY", "")
SENTIMENT_MODEL_NAME = os.getenv("NIVESH_SENTIMENT_MODEL_NAME", "ProsusAI/finbert")
SENTIMENT_MODEL_MAX_ARTICLES = int(os.getenv("NIVESH_SENTIMENT_MODEL_MAX_ARTICLES", "6"))


def validate_runtime_security() -> None:
    """Fail closed before a production process can serve traffic."""
    if not IS_PRODUCTION:
        return
    failures = []
    if len(JWT_SECRET) < 32 or JWT_SECRET == "change-this-secret-before-deployment":
        failures.append("NIVESH_SECRET must be a unique random value of at least 32 characters")
    if len(MODEL_ARTIFACT_KEY) < 32 or MODEL_ARTIFACT_KEY == JWT_SECRET:
        failures.append("NIVESH_MODEL_ARTIFACT_KEY must be a separate random value of at least 32 characters")
    if len(BROKER_SESSION_KEY) < 32 or BROKER_SESSION_KEY in {JWT_SECRET, MODEL_ARTIFACT_KEY}:
        failures.append("NIVESH_BROKER_SESSION_KEY must be a third independent random value of at least 32 characters")
    if not REDIS_URL.startswith("rediss://"):
        failures.append("a TLS REDIS_URL is required")
    if not COOKIE_SECURE:
        failures.append("NIVESH_COOKIE_SECURE=1 is required")
    if DEMO_MODE:
        failures.append("NIVESH_DEMO_MODE must be 0")
    if SIGNUP_ENABLED:
        failures.append("NIVESH_SIGNUP_ENABLED must be 0 until an approved onboarding flow exists")
    if not DATABASE_URL.startswith("postgresql"):
        failures.append("a PostgreSQL DATABASE_URL is required")
    if not ALLOWED_ORIGINS or any(not origin.startswith("https://") for origin in ALLOWED_ORIGINS):
        failures.append("NIVESH_ALLOWED_ORIGINS must contain only explicit HTTPS origins")
    if not ADMIN_EMAILS:
        failures.append("NIVESH_ADMIN_EMAILS must name at least one operations administrator")
    if not COGNITO_AUTH_REQUIRED:
        failures.append("NIVESH_COGNITO_AUTH_REQUIRED=1 is required")
    if not TRUSTED_ALB_ARN.startswith("arn:aws:elasticloadbalancing:"):
        failures.append("NIVESH_TRUSTED_ALB_ARN must identify the HTTPS application load balancer")
    if not COGNITO_LOGOUT_URL.startswith("https://"):
        failures.append("NIVESH_COGNITO_LOGOUT_URL must be an HTTPS Cognito logout endpoint")
    if failures:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(failures))
