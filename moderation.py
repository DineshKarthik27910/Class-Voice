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

_FALLBACK_RESULT = {
    "category": "POTENTIALLY_ABUSIVE",
    "reason": "AI moderation unavailable — flagged for manual review",
    "confidence": 0.0,
}

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
            category: str — one of SAFE, POTENTIALLY_ABUSIVE, SEVERE_VIOLATION
            reason: str — brief explanation from the AI
            confidence: float — 0.0 to 1.0

    Fallback: On API failure/timeout/missing key, returns POTENTIALLY_ABUSIVE
    so the message is broadcast but flagged for admin review.
    """
    if not text or not text.strip():
        return {"category": "SAFE", "reason": "Empty message", "confidence": 1.0}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "[MODERATION FALLBACK] GEMINI_API_KEY not set — "
            "flagging as POTENTIALLY_ABUSIVE for admin review",
            flush=True,
        )
        return dict(_FALLBACK_RESULT)

    try:
        result = _call_gemini(text, api_key)
        return result
    except Exception as e:
        print(
            f"[MODERATION FALLBACK] AI moderation failed ({type(e).__name__}: {e}) — "
            f"flagging as POTENTIALLY_ABUSIVE for admin review",
            flush=True,
        )
        return dict(_FALLBACK_RESULT)


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
