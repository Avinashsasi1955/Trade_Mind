import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_PATH = Path(os.getenv("NIVESH_DATABASE", DATA_DIR / "nivesh.db"))
JWT_SECRET = os.getenv("NIVESH_SECRET", "change-this-secret-before-deployment")
TOKEN_TTL_SECONDS = int(os.getenv("NIVESH_TOKEN_TTL", "86400"))
DEFAULT_CAPITAL = 1_000_000.0
HOST = os.getenv("NIVESH_HOST", "127.0.0.1")
PORT = int(os.getenv("NIVESH_PORT", "4173"))
LLM_BASE_URL = os.getenv("NIVESH_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("NIVESH_LLM_API_KEY", "")
LLM_MODEL = os.getenv("NIVESH_LLM_MODEL", "")
MODEL_PROVIDER = os.getenv("NIVESH_MODEL_PROVIDER", "auto").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "0") == "1"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
AI_GATEWAY_RATE_LIMIT = int(os.getenv("AI_GATEWAY_RATE_LIMIT", "10"))
AI_GATEWAY_CACHE_SECONDS = int(os.getenv("AI_GATEWAY_CACHE_SECONDS", "600"))
AI_GATEWAY_CIRCUIT_SECONDS = int(os.getenv("AI_GATEWAY_CIRCUIT_SECONDS", "120"))
