"""Gate 0: is this text even language?

Deterministic, ~1ms, no API call. Exists because feedback in this system is
*persistent* — a garbage entry that gets through is injected into every future
iteration forever. The cost asymmetry justifies a strict front door.

Four independent signals, because any one alone has a failure mode:
  - dictionary hit rate   -> misses valid jargon, so we whitelist schema terms
  - vowel ratio           -> misses "aeiou", catches "hygyt"
  - consonant run length  -> catches "hjkghtr", misses "aeiou"
  - bigram log-likelihood -> catches keyboard mash generally

A submission fails if it trips ENOUGH signals (see G0_FAIL_THRESHOLD), not just
one, so a legitimate terse comment full of column names doesn't get bounced.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

import config

# Strict vowels: 'y' is deliberately excluded. Only unknown tokens are ever
# scored, so treating 'y' as a consonant costs us nothing on real words like
# "rhythm" (dictionary hit, never scored) while closing the loophole that let
# "hygyt" past with an apparent 0.4 vowel ratio.
VOWELS = set("aeiou")
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALPHA_RE = re.compile(r"^[A-Za-z]+$")

# Terms that are legitimate feedback vocabulary but absent from a dictionary.
DOMAIN_LEXICON = {
    "sql", "null", "nulls", "nullable", "dq", "poc", "pct", "regex", "utc",
    "varchar", "int", "bigint", "decimal", "boolean", "timestamp", "datetime",
    "pk", "fk", "dedupe", "dedup", "upsert", "backfill", "schema", "colum",
    "config", "param", "params", "api", "json", "csv", "etl", "kpi", "uuid",
    "isnull", "isblank", "notnull", "coalesce", "cast", "substr", "todo",
}

# How many signals must agree on a single token before it counts as garbage.
G0_FAIL_THRESHOLD = 2

# What fraction of the comment's words must be garbage before we reject it.
# 0.4 rejects "asdfgh qwerty" (qwerty is in the dictionary, asdfgh is not)
# while tolerating one mashed token inside an otherwise real sentence.
G0_GARBAGE_RATIO = 0.4

# Trained lazily off the system wordlist; cached for the process lifetime.
_BIGRAM_CACHE: dict | None = None


@lru_cache(maxsize=1)
def _load_wordlist() -> frozenset[str]:
    path = config.WORDLIST_PATH
    if not path.exists():
        return frozenset()
    words = set()
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            w = line.strip().lower()
            if w and "'" not in w:
                words.add(w)
    return frozenset(words)


def _bigram_model() -> dict:
    """P(bigram) over the system wordlist, in log10, with add-1 smoothing."""
    global _BIGRAM_CACHE
    if _BIGRAM_CACHE is not None:
        return _BIGRAM_CACHE

    counts: Counter = Counter()
    total = 0
    for w in _load_wordlist():
        if not ALPHA_RE.match(w) or len(w) < 3:
            continue
        padded = f"^{w}$"
        for i in range(len(padded) - 1):
            counts[padded[i : i + 2]] += 1
            total += 1

    vocab = 28 * 28  # a-z plus ^ and $
    floor = math.log10(1.0 / (total + vocab)) if total else -6.0
    model = {
        "logp": {bg: math.log10((c + 1) / (total + vocab)) for bg, c in counts.items()},
        "floor": floor,
    }
    _BIGRAM_CACHE = model
    return model


def _bigram_score(word: str) -> float:
    """Mean log10 bigram probability. Real words ~ -2.4; mash ~ -4.5."""
    model = _bigram_model()
    if not model["logp"]:
        return 0.0  # no wordlist available -> signal disabled
    padded = f"^{word.lower()}$"
    scores = [
        model["logp"].get(padded[i : i + 2], model["floor"])
        for i in range(len(padded) - 1)
    ]
    return sum(scores) / len(scores) if scores else model["floor"]


def _max_consonant_run(word: str) -> int:
    run = best = 0
    for ch in word.lower():
        if ch.isalpha() and ch not in VOWELS:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _vowel_ratio(word: str) -> float:
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c in VOWELS) / len(letters)


def check_coherence(text: str, schema_terms: set[str] | None = None) -> dict:
    """Return {passed, reason, signals, detail}.

    schema_terms: column/table names for the table under review. Passing these
    stops 'gross_total must net off rebate' from being scored as gibberish.
    """
    schema_terms = {t.lower() for t in (schema_terms or set())}
    # Column names are compound; index their parts too (gross_total -> gross, total).
    schema_parts = set(schema_terms)
    for term in schema_terms:
        schema_parts.update(p for p in term.split("_") if p)

    stripped = text.strip()
    detail: dict = {"length": len(stripped)}

    if len(stripped) < config.G0_MIN_CHARS:
        return {
            "passed": False,
            "reason": f"too short ({len(stripped)} chars, minimum {config.G0_MIN_CHARS}) "
            f"— feedback must say what is wrong and why",
            "signals": ["length"],
            "detail": detail,
        }

    printable = sum(1 for c in stripped if c.isprintable())
    if printable / len(stripped) < 0.95:
        return {
            "passed": False,
            "reason": "contains non-printable characters",
            "signals": ["encoding"],
            "detail": detail,
        }

    tokens = TOKEN_RE.findall(stripped)
    alpha_tokens = [t for t in tokens if ALPHA_RE.match(t) and len(t) >= 3]
    detail["tokens"] = len(tokens)
    detail["alpha_tokens"] = len(alpha_tokens)

    if len(tokens) < config.G0_MIN_ALPHA_TOKENS:
        return {
            "passed": False,
            "reason": f"only {len(tokens)} word(s) — too terse to act on",
            "signals": ["token_count"],
            "detail": detail,
        }

    if not alpha_tokens:
        return {
            "passed": True,  # e.g. a pure expression like "qty*price - discount"
            "reason": "no prose tokens to score (expression-only comment)",
            "signals": [],
            "detail": detail,
        }

    words = _load_wordlist()
    known, unknown = [], []
    for t in alpha_tokens:
        low = t.lower()
        if low in words or low in DOMAIN_LEXICON or low in schema_parts:
            known.append(t)
        else:
            unknown.append(t)

    hit_rate = len(known) / len(alpha_tokens)
    detail["dict_hit_rate"] = round(hit_rate, 3)
    detail["unknown_tokens"] = unknown

    # Hard rule: if not one token is a known word, a schema term or domain
    # jargon, there is nothing to act on regardless of character statistics.
    # This is what catches repeated mash like "hygyt hygyt", whose bigram and
    # vowel profile alone are not damning enough to trip two signals.
    if words and hit_rate == 0.0 and len(alpha_tokens) >= 2:
        return {
            "passed": False,
            "reason": (
                "no recognisable words — none of "
                f"{', '.join(repr(t) for t in alpha_tokens[:5])} is a known word, "
                "a column in this table, or known jargon"
            ),
            "signals": ["dictionary", "no_known_tokens"],
            "detail": detail,
        }

    # Per-token scoring. Only unrecognised tokens are scored; known words would
    # dilute the average and let a single mashed token hide behind them.
    lo, hi = config.G0_VOWEL_RATIO_RANGE
    per_token: dict[str, list[str]] = {}
    stats: dict[str, dict] = {}

    for t in unknown:
        fired = []
        bg = _bigram_score(t)
        run = _max_consonant_run(t)
        vr = _vowel_ratio(t)
        if bg < config.G0_MIN_BIGRAM_SCORE:
            fired.append("bigram")
        if run > config.G0_MAX_CONSONANT_RUN:
            fired.append("consonant_run")
        if not (lo <= vr <= hi):
            fired.append("vowel_ratio")
        # Length 3 is safe here because only unknown tokens reach this loop —
        # real 2-unique-char words ("see", "all", "too") are dictionary hits
        # and are never scored.
        if len(t) >= 3 and len(set(t.lower())) <= 2:
            fired.append("low_char_variety")
        per_token[t] = fired
        stats[t] = {"bigram": round(bg, 3), "run": run, "vowel_ratio": round(vr, 3)}

    detail["token_stats"] = stats
    detail["token_signals"] = per_token

    # A token is "garbage" once two independent signals agree on it.
    garbage = [t for t, sigs in per_token.items() if len(sigs) >= G0_FAIL_THRESHOLD]
    garbage_ratio = len(garbage) / len(alpha_tokens)
    detail["garbage_tokens"] = garbage
    detail["garbage_ratio"] = round(garbage_ratio, 3)

    all_signals = sorted({s for sigs in per_token.values() for s in sigs})
    if words and hit_rate < config.G0_MIN_DICT_HIT_RATE:
        all_signals.append("dictionary")
    detail["signals_fired"] = all_signals

    if garbage and garbage_ratio >= G0_GARBAGE_RATIO:
        bad = ", ".join(f"'{t}'" for t in garbage[:5])
        return {
            "passed": False,
            "reason": (
                f"does not read as language — {bad} "
                f"fail {'/'.join(sorted({s for t in garbage for s in per_token[t]}))}"
            ),
            "signals": all_signals,
            "detail": detail,
        }

    return {
        "passed": True,
        "reason": "coherent",
        "signals": all_signals,
        "detail": detail,
    }
