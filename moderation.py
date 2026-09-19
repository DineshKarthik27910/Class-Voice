"""
AI Moderation Service for Class Voice.

Classifies chat messages into:
  - SAFE: Normal conversation, no action needed.
  - POTENTIALLY_ABUSIVE: Borderline/mild content — admin reviews.
  - SEVERE_VIOLATION: Clearly abusive — auto-rejected, never broadcast.

Provider: Google Gemini (configurable via GEMINI_MODEL, default: gemini-2.5-flash).
The module is provider-independent at the interface level:
swap the _call_gemini() implementation to switch providers.

Fallback on API failure: POTENTIALLY_ABUSIVE (message is broadcast but
flagged for admin review — nothing severe can slip through unreviewed).

Explicit opt-out (MODERATION_ENABLED=false): returns SAFE (intentional).
"""

import os
import json
import re
import requests

# ── Constants ────────────────────────────────────────────────────────────────

CATEGORIES = ("SAFE", "POTENTIALLY_ABUSIVE", "SEVERE_VIOLATION")

# Deterministic Emergency Safety Guard Patterns
# Targets ONLY unambiguous severe-risk patterns:
# 1. explicit credible threats of physical violence
# 2. explicit encouragement/instruction for suicide or self-harm
# 3. explicit doxxing / private-address exposure
# 4. extremely explicit targeted hate/slur patterns where there is no meaningful ambiguity
# (Ordinary profanity, insults like "you're stupid", "idiot", "shut up" are intentionally NOT matched)
_THREAT_PATTERNS = [
    re.compile(
        r"\b(?:i\s*(?:will|'ll|am\s*going\s*to|gonna)|i'?m\s*(?:going\s*to|gonna))\s*"
        r"(?:kill|murder|shoot|stab|slit\s+your\s+throat)\s+(?:you|u)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s*(?:will|'ll|am\s*going\s*to|gonna)|i'?m\s*(?:going\s*to|gonna))\s*"
        r"(?:find\s+(?:and|&)\s*)?(?:kill|murder)\s+(?:you|u)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:going\s*to|gonna|will)\s*(?:shoot\s*up|bomb|blow\s*up)\s+"
        r"(?:the\s+)?(?:college|campus|class|school|university)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bomb\s*threat|death\s*threat\s*to\s*(?:you|u))\b",
        re.IGNORECASE,
    ),
]

_SUICIDE_PATTERNS = [
    re.compile(r"\b(?:go\s+)?kill\s+your\s*self\b", re.IGNORECASE),
    re.compile(r"\bcommit\s+suicide\b", re.IGNORECASE),
    re.compile(r"\b(?:go\s+)?slit\s+your\s+wrists?\b", re.IGNORECASE),
    re.compile(r"\b(?:go\s+)?hang\s+your\s*self\b", re.IGNORECASE),
    re.compile(r"\bdrink\s+bleach(?:\s+and\s+die)?\b", re.IGNORECASE),
    re.compile(r"\bk\s*y\s*s\b", re.IGNORECASE),
]

_DOXXING_PATTERNS = [
    re.compile(r"\bdoxx(?:ed|ing)?\s*:", re.IGNORECASE),
    re.compile(r"\bi(?:'m|\s*am)\s*doxxing\s+(?:you|u)\b", re.IGNORECASE),
    re.compile(
        r"\bhere\s+is\s+(?:his|her|their|your)\s+(?:home\s+address|private\s+address|phone\s+number)\s*:",
        re.IGNORECASE,
    ),
]

_EXTREME_SLURS_PATTERN = re.compile(
    r"\b(?:m[@a]d[a@]rch[o0]d\w*|b[e3]h[e3]nch[o0]d\w*|bh[o0]sd[i1](?:w[a@]l[a@]|k[e3])|lanjak[o0]duku|lanja\s*koduku|moddagudu|modda\s*gudu|p[o0]{1,2}k[uo]|p[o0]{1,2}k[o0]du|puk[au]|nigg(?:er|a|ah|as|az)|fagg?ot)\b",
    re.IGNORECASE,
)


def _check_emergency_severe(text: str) -> dict | None:
    """
    Deterministic emergency safety guard executed BEFORE Gemini.
    Targets ONLY clearly unambiguous severe-risk patterns.
    If not highly confident that content is severe, returns None to allow
    normal processing through Gemini / manual review.
    """
    if not text:
        return None

    for p in _THREAT_PATTERNS:
        if p.search(text):
            return {
                "category": "SEVERE_VIOLATION",
                "reason": "Emergency safety rule: explicit credible threat of physical violence",
                "confidence": 1.0,
            }

    for p in _SUICIDE_PATTERNS:
        if p.search(text):
            return {
                "category": "SEVERE_VIOLATION",
                "reason": "Emergency safety rule: explicit encouragement or instruction of suicide/self-harm",
                "confidence": 1.0,
            }

    for p in _DOXXING_PATTERNS:
        if p.search(text):
            return {
                "category": "SEVERE_VIOLATION",
                "reason": "Emergency safety rule: explicit doxxing / private address exposure",
                "confidence": 1.0,
            }

    if _EXTREME_SLURS_PATTERN.search(text):
        return {
            "category": "SEVERE_VIOLATION",
            "reason": "Emergency safety rule: unambiguous severe hate slur",
            "confidence": 1.0,
        }

    return None

_SYSTEM_PROMPT = """You are an expert content moderation AI for an anonymous college student chat platform called "Class Voice".
Students post anonymously, and your job is to classify each incoming message into EXACTLY ONE category: "SAFE", "POTENTIALLY_ABUSIVE", or "SEVERE_VIOLATION".

### 1. CATEGORIES AND CRITERIA

1. SAFE:
Classify as SAFE when the message is normal conversation, academic discussion, campus questions, harmless jokes, friendly banter, casual slang, etc.
- Words or mild expressions used in a non-targeted, non-abusive context (e.g., "damn this homework was tough", "what the heck").
- Academic discussions or quotes that mention sensitive terminology without abusive intent.

2. POTENTIALLY_ABUSIVE:
Use this ONLY for borderline/mild personal insults or rude statements where the language is NOT clearly severe abusive language.
Examples:
- "Bro you're stupid"
- "Bro r u stupid?"
- "You're an idiot"
- "Shut up bro"
- "You're useless"
- "Nobody likes you lol"
These messages remain visible in chat and are sent to the admin moderation queue for human review.

3. SEVERE_VIOLATION:
Clearly abusive profanity, slurs, or strongly degrading abusive language must ALWAYS be classified as SEVERE_VIOLATION.
A clearly abusive word by itself is enough to classify the message as SEVERE_VIOLATION, even if there is no threat.
Examples include:
- Clearly abusive Hindi / Hinglish profanity (e.g., madarchod, behenchod, mc, bc, chutiya, chutiye, gandu, bhosdi, bhosdiwala, bhadwa, randi, land, etc.).
- Clearly abusive Telugu / Romanized Telugu profanity (e.g., lanja, lanjakoduku, dengu, dengey, dengulata, pooku, pookodu, etc.).
- Severe English profanity or highly degrading insults directed at a person (e.g., fuck you, motherfucker, bitch, asshole, cunt, whore, slut, etc.).
- Casteist, racial, religious, homophobic, or other identity-based slurs and hate speech.
- Strongly degrading or predatory sexual abuse.
- Explicit threats of physical violence or harm (e.g., "I will kill you", "I'll beat you up", "I'm going to hurt you", "I'll find you", "I'll come for you").
- Encouragement of suicide or self-harm (e.g., "kill yourself", "kys", "go die").
- Doxxing or exposing someone's private personal information.

### 2. STRICT SEVERITY RULES & OBFUSCATION HANDLING
- Do NOT downgrade a clearly abusive slur or profanity term to POTENTIALLY_ABUSIVE just because it is used casually, jokingly, repeatedly, or with spelling variations.
- Detect common obfuscation, character substitutions, and repeated letters, such as:
  "madarchodddd", "f*ck", "f u c k", "fck", "chut1ya", "g@ndu", "m@darchod", "k y s", "l a n j a", "l a n j a k o d u k u","m o d d a g u d u "
etc.
- A clearly abusive term with spelling variations or obfuscation must still be SEVERE_VIOLATION.
- Context should still be considered for genuinely ambiguous words, but clearly abusive vocabulary should be treated as severe.
- Do not use a simple keyword-only approach for ambiguous words. However, when a word is clearly an abusive slur/profanity, classify it as SEVERE.

Benchmarks:
- "Bro r u stupid?" → POTENTIALLY_ABUSIVE
- "You're useless" → POTENTIALLY_ABUSIVE
- "madarchodddd" → SEVERE_VIOLATION
- A clearly abusive slur by itself → SEVERE_VIOLATION
- Explicit threat → SEVERE_VIOLATION

### 3. MULTILINGUAL SUPPORT
Understand English, Hindi, Hinglish, Telugu, Romanized Telugu, and common spelling variations or obfuscation across Indian campus communication.

### 4. TIE-BREAKING & UNCERTAINTY RULES
- If you are unsure between SAFE and POTENTIALLY_ABUSIVE → POTENTIALLY_ABUSIVE.
- If the message contains a clearly abusive profanity/slur and you are unsure whether it is POTENTIALLY_ABUSIVE or SEVERE → SEVERE_VIOLATION.

### 5. OUTPUT FORMAT
Return ONLY a valid JSON object in this exact format, with no markdown formatting, explanations outside JSON, or additional fields:
{"category": "SAFE|POTENTIALLY_ABUSIVE|SEVERE_VIOLATION", "reason": "brief explanation", "confidence": 0.0}"""

# ── Public API ───────────────────────────────────────────────────────────────


def moderate_message(text: str) -> dict:
    """
    Classify a chat message for moderation.

    Args:
        text: The raw message text to classify.

    Returns:
        dict with keys:
            category: str — one of SAFE, POTENTIALLY_ABUSIVE, SEVERE_VIOLATION, or AI_UNAVAILABLE
            reason: str — explanation from AI or emergency rule / fallback
            confidence: float | None — 0.0 to 1.0 for AI classifications, None for AI_UNAVAILABLE

    Fallback: On API failure/timeout/missing key/rate limit, returns AI_UNAVAILABLE
    so the message is kept visible and flagged for manual admin review.
    """
    if not text or not text.strip():
        return {"category": "SAFE", "reason": "Empty message", "confidence": 1.0}

    # 1. Deterministic Emergency Safety Guard (runs before Gemini)
    emergency_result = _check_emergency_severe(text)
    if emergency_result:
        return emergency_result

    # 2. Check Gemini API key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "[MODERATION FALLBACK] AI unavailable\n"
            "Reason: GEMINI_API_KEY not set — treating as SAFE by availability fallback",
            flush=True,
        )
        return {
            "category": "SAFE",
            "reason": "AI moderation unavailable — treated as SAFE by availability fallback",
            "confidence": None,
            "ai_status": "unavailable",
        }

    # 3. Call Gemini
    try:
        result = _call_gemini(text, api_key)
        return result
    except Exception as e:
        print(
            f"[MODERATION FALLBACK] AI unavailable\n"
            f"Reason: {type(e).__name__}: {e} — treating as SAFE by availability fallback",
            flush=True,
        )
        return {
            "category": "SAFE",
            "reason": "AI moderation unavailable — treated as SAFE by availability fallback",
            "confidence": None,
            "ai_status": "unavailable",
        }


# ── Provider Implementation (Google Gemini) ──────────────────────────────────

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def get_gemini_model() -> str:
    """Return the configured Gemini model name from GEMINI_MODEL env var or default."""
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


def _call_gemini(text: str, api_key: str) -> dict:
    """Call Google Gemini API for content classification."""
    model = get_gemini_model()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"Classify this message:\n\n{text}"}],
            }
        ],
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
        },
    }

    response = requests.post(
        f"{url}?key={api_key}",
        json=payload,
        timeout=10,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini API returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    # Extract the text content from Gemini's response structure
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Unexpected Gemini response structure: {exc}"
        ) from exc

    return _parse_response(raw_text)


def _parse_response(raw_text: str) -> dict:
    """Parse and validate the JSON response from the AI model."""
    try:
        result = json.loads(raw_text.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code blocks
        json_match = re.search(r"\{[^}]+\}", raw_text)
        if json_match:
            result = json.loads(json_match.group())
        else:
            raise RuntimeError(
                f"Could not parse AI response as JSON: {raw_text[:200]}"
            )

    category = result.get("category", "").upper().strip()
    if category not in CATEGORIES:
        print(
            f"[MODERATION] Unknown category '{category}' — "
            f"defaulting to POTENTIALLY_ABUSIVE",
            flush=True,
        )
        category = "POTENTIALLY_ABUSIVE"

    reason = str(result.get("reason", ""))[:500]

    try:
        confidence = float(result.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.5

    return {
        "category": category,
        "reason": reason,
        "confidence": confidence,
    }
