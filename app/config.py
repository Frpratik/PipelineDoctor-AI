"""Application settings, loaded from the .env file (same python-dotenv style as VideoFit AI)."""

import os

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")  # change in .env to switch models
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# Which Investigator prompt to use (see PROMPTS in app/agents/investigator.py). Evals switch this to compare prompts.
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
FALLBACK_ALLOWED = True   # evals turn this off so a rate limit cannot silently swap the model being tested

# Optional fallback LLM, used when Groq says we are rate limited (free key: https://aistudio.google.com/apikey)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-2.5-flash")  # check the current name in Google AI Studio

POSTGRES_USER =os.getenv("POSTGRES_USER", "doctor")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "doctor")
POSTGRES_DB = os.getenv("POSTGRES_DB", "pipeline_doctor")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")  # becomes "postgres" inside Docker (Step 11)
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def require_groq_key() -> str:
    """Return the Groq API key or raise a clear error if it is missing (same helper as VideoFit AI)."""
    if not GROQ_API_KEY or GROQ_API_KEY == "your-groq-key":
        raise RuntimeError("GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
    return GROQ_API_KEY


def fallback_enabled() -> bool:
    return FALLBACK_ALLOWED and bool(GEMINI_API_KEY) and GEMINI_API_KEY != "your-gemini-key"


def get_db_url() -> str:
    """One connection string for the whole project."""
    return f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
