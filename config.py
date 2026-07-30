"""Central config. One place to change models, thresholds and paths."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
DB_PATH = ROOT / "feedback.db"
CHROMA_DIR = ROOT / "chroma"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Split mirrors the prod codebase's _llm_main / _llm_mini pattern.
# NOTE: gemini-3.x rejects temperature/top_p/top_k. Determinism is handled by
# freezing v1 to disk and measuring a noise floor, not by sampling params.
GEN_MODEL = os.getenv("GEN_MODEL", "gemini-3.6-flash")
VALIDATOR_MODEL = os.getenv("VALIDATOR_MODEL", "gemini-3.5-flash-lite")

# gemini-embedding-001 (not -2) because only it supports task_type, which gives
# us asymmetric retrieval: RETRIEVAL_DOCUMENT on write, RETRIEVAL_QUERY on read.
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
EMBED_TASK_DOC = "RETRIEVAL_DOCUMENT"
EMBED_TASK_QUERY = "RETRIEVAL_QUERY"

# --- Validation gate thresholds -------------------------------------------
# Gate 0 (coherence, deterministic)
G0_MIN_CHARS = 8
G0_MIN_ALPHA_TOKENS = 2
G0_MIN_DICT_HIT_RATE = 0.5
G0_MAX_CONSONANT_RUN = 4
G0_VOWEL_RATIO_RANGE = (0.12, 0.70)
# Mean log10 P(bigram), calibrated against /usr/share/dict/american-english:
# real words p1=-2.96, p5=-2.71, median=-2.35; keyboard mash spans -5.1..-2.9.
# -2.90 sits at ~1% false-fire on real words, which is safe because a token
# must trip TWO signals to count as garbage.
G0_MIN_BIGRAM_SCORE = -2.90

# Gate 2 (semantic, LLM)
G2_MIN_CONFIDENCE = 0.70

# Chroma cross-table transfer. Cosine distance; lower is closer.
# Tune on the two synthetic tables — see docs/THRESHOLD_TUNING.md.
VECTOR_DISTANCE_THRESHOLD = 0.45

# --- Anomaly feedback ------------------------------------------------------
# Rules are time-invariant so their feedback never expires. Profiling anomalies
# are not: a column's missing-rate distribution drifts, so a dismissal made a
# year ago should not still be silencing an alert today.
ANOMALY_FEEDBACK_TTL_DAYS = 90

# When a user dismisses an anomaly without naming a bound, suppress only up to
# the magnitude they actually saw, plus this headroom (percentage points).
# Dismissing a 64% missing-rate alert must NOT hide the same column at 99%.
ANOMALY_DEFAULT_BOUND_HEADROOM = 5.0

# Dismissing the same anomaly this many times means the detector is
# miscalibrated, not that the user needs asking again. Surfaced, not enforced.
ANOMALY_DISMISS_ESCALATION = 3

WORDLIST_PATH = Path("/usr/share/dict/american-english")

for _d in (RUNS_DIR, CHROMA_DIR):
    _d.mkdir(exist_ok=True)
