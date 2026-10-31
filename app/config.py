"""Application settings, read from the environment with safe defaults."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")  # no-op in Docker, where env vars come from --env-file

# LLM (any OpenAI-compatible Chat Completions endpoint with tool calling)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
# gemini-3.1-flash-lite: the model the evaluation ran on; free-tier quotas are per
# model, and gemini-3.5-flash allowed only 20 requests/day when tested.
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOOL_ROUNDS = int(os.getenv("LLM_MAX_TOOL_ROUNDS", "6"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "4"))  # SDK retries 429/5xx with exponential backoff
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "40"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()  # WARNING shows rate-limit waits and retries
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))

DATASET_PATH = Path(os.getenv("DATASET_PATH", DATA_DIR / "corfu_dataset.json"))
RAIN_FIXTURE_PATH = DATA_DIR / "corfu_synthetic_rain.json"

INDEX_PATH = Path(os.getenv("INDEX_PATH", ROOT_DIR / "storage" / "poi_index.npz"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_CACHE_DIR = Path(os.getenv("EMBEDDING_CACHE_DIR", ROOT_DIR / ".cache" / "fastembed"))

TIMEZONE_NAME = "Europe/Athens"
TZ = ZoneInfo(TIMEZONE_NAME)

# Walking model (see README, "Walking time model")
WALK_SPEED_KMH = float(os.getenv("WALK_SPEED_KMH", "4.5"))
WALK_SPEED_WITH_CHILD_KMH = float(os.getenv("WALK_SPEED_WITH_CHILD_KMH", "3.5"))
DETOUR_FACTOR = float(os.getenv("DETOUR_FACTOR", "1.3"))
MAX_WALK_LEG_KM = float(os.getenv("MAX_WALK_LEG_KM", "3.0"))

# Planner
TRANSITION_BUFFER_MIN = 5  # finding the entrance, tickets, orientation, per walking leg
MAX_WAIT_MIN = 30  # longest acceptable wait for a place to open
MAX_STOPS = 6
# New plans finish this many minutes early (windows >= PLAN_RESERVE_MIN_WINDOW only),
# leaving room for delays and for later revisions such as a slower child's pace.
# Revisions may use the full window.
PLAN_RESERVE_MIN = 10
PLAN_RESERVE_MIN_WINDOW = 120
MIN_WINDOW_MIN = 30
MAX_WINDOW_MIN = 10 * 60
RAIN_THRESHOLD_MM = 0.5  # hourly precipitation treated as rain
