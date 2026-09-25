"""Non-secret preferences and validated OpenRouter answer settings."""
import json
import math
import re
from pathlib import Path

import requests
from Foundation import NSUserDefaults

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_METADATA = {"id": DEFAULT_MODEL, "name": "Claude Haiku 4.5",
                    "supported_parameters": ["max_tokens", "temperature", "top_p", "top_k", "response_format"]}
# A separate suite keeps CLI and bundle preferences consistent without moving existing keys.
PREFS = NSUserDefaults.alloc().initWithSuiteName_("com.heyjev.preferences")
CACHE = Path.home() / "Library/Caches/com.heyjev.app/models.json"
# label, numeric type, inclusive lower/upper bounds; blank means provider default.
PARAMETERS = {
    "max_tokens": ("Output token limit", int, 1, 1000000),
    "temperature": ("Temperature", float, 0, 2),
    "top_p": ("Top P", float, 0.000001, 1),
    "top_k": ("Top K", int, 0, 1000000),
    "frequency_penalty": ("Frequency penalty", float, -2, 2),
    "presence_penalty": ("Presence penalty", float, -2, 2),
    "seed": ("Seed", int, -2147483648, 2147483647),
}


def answer_settings():
    raw = PREFS.stringForKey_("answer_settings")
    try:
        value = json.loads(raw) if raw else {}
        model = value.get("model", DEFAULT_MODEL)
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/+-]+", model):
            raise ValueError("Invalid model")
        metadata = value.get("metadata", DEFAULT_METADATA if model == DEFAULT_MODEL else {})
        parameters = validate_parameters(value.get("parameters", {}), metadata)
        return {"model": model, "parameters": parameters, "metadata": metadata}
    except (ValueError, TypeError, AttributeError):
        return {"model": DEFAULT_MODEL, "parameters": {}, "metadata": DEFAULT_METADATA}


def validate_parameters(values, metadata):
    supported = set(metadata.get("supported_parameters", []))
    result = {}
    for key, raw in values.items():
        if raw is None or str(raw).strip() == "":
            continue
        if key not in PARAMETERS or key not in supported:
            raise ValueError(f"{key} is not supported by this model.")
        title, convert, low, high = PARAMETERS[key]
        try:
            if isinstance(raw, bool):
                raise ValueError()
            value = convert(str(raw).strip())
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError()
        except (ValueError, OverflowError):
            raise ValueError(f"{title}: enter {'an integer' if convert is int else 'a number'} from {low:g} to {high:g}.") from None
        if key == "max_tokens":
            limits = [metadata.get("context_length"), (metadata.get("top_provider") or {}).get("max_completion_tokens")]
            limit = min((n for n in limits if isinstance(n, int) and n > 0), default=high)
            if value > limit:
                raise ValueError(f"Output token limit must be at most {limit:,} for this model.")
        result[key] = value
    return result


def apple_model():
    """Which of Apple's built-in models answers questions: "on_device" (default) or "private_cloud"."""
    value = PREFS.stringForKey_("apple_model")
    return value if value in ("on_device", "private_cloud") else "on_device"


def save_apple_model(value):
    if value not in ("on_device", "private_cloud"):
        raise ValueError("Unknown Apple model.")
    PREFS.setObject_forKey_(value, "apple_model")


AGENTS = ("claude", "codex")  # agent sessions that can answer: Claude Code, Codex


def agent_settings(name):
    """Where an agent session runs: {"cwd": folder}. Home unless a saved folder still exists."""
    import os
    saved = PREFS.stringForKey_(f"agent_cwd_{name}")
    return {"cwd": saved if saved and os.path.isdir(saved) else str(Path.home())}


def clean_agent_folder(cwd):
    """-> the real absolute path of an existing folder. Raises ValueError otherwise."""
    import os
    real = os.path.realpath(os.path.expanduser(str(cwd or "")))
    if not cwd or not os.path.isdir(real):
        raise ValueError(f"Working folder isn't a folder: {cwd}")
    return real


def save_agent_settings(name, cwd):
    """Save an agent's working folder. -> the stored path."""
    if name not in AGENTS:
        raise ValueError("Unknown agent.")
    real = clean_agent_folder(cwd)
    PREFS.setObject_forKey_(real, f"agent_cwd_{name}")
    return real


def save_answer_settings(model, values, metadata):
    if metadata.get("id") != model:
        raise ValueError("Refresh models and select a model before saving.")
    value = {"model": model, "parameters": validate_parameters(values, metadata), "metadata": metadata}
    PREFS.setObject_forKey_(json.dumps(value), "answer_settings")


def answer_payload(messages, *, reminder=False):
    settings = answer_settings()
    metadata = settings["metadata"]
    supported = set(metadata.get("supported_parameters", []))
    payload = {"model": settings["model"], "messages": messages, **settings["parameters"]}
    # Preserve the old 80/120 budgets until changed; user overrides apply to both paths.
    if "max_tokens" in supported:
        payload.setdefault("max_tokens", 120 if reminder else 80)
    if "reasoning" in supported and not any(k in settings["parameters"] for k in ("reasoning", "reasoning_effort")):
        payload["reasoning"] = {"effort": "minimal"}  # else thinking can spend the whole short budget, content empty
    if reminder and "response_format" in supported:
        payload["response_format"] = {"type": "json_object"}
    if not reminder:
        payload["usage"] = {"include": True}
    if settings["parameters"]:
        payload["provider"] = {"require_parameters": True}
    return payload


def cached_models():
    try:
        return normalize_models(json.loads(CACHE.read_text()))
    except (OSError, ValueError, TypeError):
        return []


def normalize_models(data):
    if not isinstance(data, list):
        raise ValueError("OpenRouter returned an invalid model catalog.")
    fields = ("id", "name", "context_length", "supported_parameters", "top_provider", "pricing")
    models = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        architecture = entry.get("architecture") or {}
        if "text" not in architecture.get("output_modalities", ["text"]):
            continue
        if "text" not in architecture.get("input_modalities", ["text"]):
            continue
        if not isinstance(entry.get("supported_parameters"), list):
            continue
        models.append({k: entry[k] for k in fields if k in entry})
    return sorted(models, key=lambda m: m["id"].lower())


def fetch_models(key):
    # Never follow API-provided URLs or redirects with a user's credential.
    r = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {key}"},
                     timeout=(5, 25), allow_redirects=False)
    if r.status_code != 200:
        raise ValueError(f"Model catalog unavailable (HTTP {r.status_code}). Check the answer key or retry.")
    models = normalize_models(r.json().get("data"))
    if not models:
        raise ValueError("OpenRouter returned no text models. Keep your saved model or retry.")
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE.with_suffix(".tmp")
        temporary.write_text(json.dumps(models))
        temporary.replace(CACHE)
    except OSError:
        pass  # Catalog use does not depend on a writable cache.
    return models


def confirm_policy():
    """Effect category -> "ask" | "auto". Unknown or missing categories fall back to the defaults."""
    from actions import DEFAULT_POLICY
    try:
        saved = json.loads(PREFS.stringForKey_("confirm_policy") or "{}")
    except ValueError:
        saved = {}
    return {k: saved.get(k) if saved.get(k) in ("ask", "auto") else v for k, v in DEFAULT_POLICY.items()}


def save_confirm_policy(policy):
    PREFS.setObject_forKey_(json.dumps({k: v for k, v in policy.items() if v in ("ask", "auto")}), "confirm_policy")


BACKENDS = ("whisper", "apple")


def transcription_backend():
    """ "whisper" (local faster-whisper, default) or "apple" (Apple on-device Speech). Takes effect at next start."""
    saved = PREFS.stringForKey_("transcription_backend")
    return saved if saved in BACKENDS else "whisper"


def save_transcription_backend(backend):
    if backend not in BACKENDS:
        raise ValueError(f"Unknown transcription backend: {backend}")
    PREFS.setObject_forKey_(backend, "transcription_backend")


def wake_settings():
    """-> (phrase, aliases). Anything invalid in storage falls back to the default phrase."""
    import wake
    try:
        saved = json.loads(PREFS.stringForKey_("wake_phrase") or "{}")
        return wake.validate(saved["phrase"]), [wake.validate(a) for a in saved.get("aliases", [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return wake.DEFAULT, []


def save_wake_settings(phrase, aliases):
    import wake
    phrase, aliases = wake.validate(phrase), [wake.validate(a) for a in aliases]
    PREFS.setObject_forKey_(json.dumps({"phrase": phrase, "aliases": aliases}), "wake_phrase")
    return phrase, aliases


TIEBREAK_DEFAULT, TIEBREAK_MIN, TIEBREAK_MAX = 85, 50, 100  # Johnny's pick; uncalibrated (jev skill)


def tiebreak_threshold():
    """Jev score (50-100) needed to pick between duplicate apps on its own. 100 means always ask."""
    try:
        v = int(PREFS.integerForKey_("tiebreak_threshold")) if PREFS.objectForKey_("tiebreak_threshold") is not None \
            else TIEBREAK_DEFAULT
    except (TypeError, ValueError):
        v = TIEBREAK_DEFAULT
    return max(TIEBREAK_MIN, min(TIEBREAK_MAX, v))


def save_tiebreak_threshold(value):
    PREFS.setInteger_forKey_(max(TIEBREAK_MIN, min(TIEBREAK_MAX, int(round(value)))), "tiebreak_threshold")


def advanced_open():
    """Whether Settings shows the Advanced answer parameters expanded. Collapsed by default."""
    return bool(PREFS.boolForKey_("settings_advanced_open"))


def save_advanced_open(value):
    PREFS.setBool_forKey_(bool(value), "settings_advanced_open")


DEFAULT_VOICE = {"id": "9a9cf47702da476aa4629e2506d4a857", "title": "Hey Jev voice"}
VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def voice():
    """-> {"id", "title"} of the Fish Audio voice Jev speaks with. Invalid storage falls back to the default."""
    try:
        saved = json.loads(PREFS.stringForKey_("fish_voice") or "{}")
        if VOICE_ID_RE.fullmatch(saved["id"]) and isinstance(saved.get("title"), str):
            return {"id": saved["id"], "title": saved["title"][:80] or saved["id"]}
    except (ValueError, KeyError, TypeError):
        pass
    return dict(DEFAULT_VOICE)


def save_voice(voice_id, title):
    if not isinstance(voice_id, str) or not VOICE_ID_RE.fullmatch(voice_id):
        raise ValueError("That voice ID isn't valid.")
    PREFS.setObject_forKey_(json.dumps({"id": voice_id, "title": str(title)[:80]}), "fish_voice")


MAX_APP_FOLDERS = 6


def app_folders():
    """User-added folders app discovery also scans (app_catalog reads the same key)."""
    raw = PREFS.arrayForKey_("app_folders")
    return [str(p) for p in raw] if raw is not None else []


def clean_app_folders(paths):
    """-> real, existing, de-duplicated absolute folders, at most MAX_APP_FOLDERS. Raises ValueError otherwise."""
    import os
    out = []
    for p in paths:
        real = os.path.realpath(os.path.expanduser(str(p)))
        if not os.path.isabs(real) or not os.path.isdir(real):
            raise ValueError(f"Not a folder: {p}")
        if real not in out:
            out.append(real)
    if len(out) > MAX_APP_FOLDERS:
        raise ValueError(f"Up to {MAX_APP_FOLDERS} extra app folders.")
    return out


def save_app_folders(paths):
    paths = clean_app_folders(paths)
    PREFS.setObject_forKey_(paths, "app_folders")
    return paths


# Models the app uses beyond the answer model. Each default is today's hard-coded value, so nothing changes until
# the user picks something.
JEV_MODELS = {"openrouter": "typesafe/jev-1.13", "typesafe": "jev-1.13.0"}  # pinned: jev-latest can change behavior
MODEL_ID_RE = re.compile(r"[A-Za-z0-9._:/+-]{1,80}")
FISH_MODELS = ("s2.1-pro-free", "s2.1-pro", "s2-pro", "s1", "drama-3-preview")  # docs.fish.audio TTS "model" header
WHISPER_SIZES = {"tiny.en": "75 MB", "base.en": "145 MB", "small.en": "480 MB", "medium.en": "1.5 GB"}
OCR_LEVELS = ("accurate", "fast")


def _choice(key, allowed, default):
    saved = PREFS.stringForKey_(key)
    return saved if saved in allowed else default


def jev_model(provider):
    saved = PREFS.stringForKey_(f"jev_model_{provider}")
    return saved if isinstance(saved, str) and MODEL_ID_RE.fullmatch(saved) else JEV_MODELS.get(provider, JEV_MODELS["typesafe"])


def save_jev_model(provider, model):
    model = (model or "").strip() or JEV_MODELS[provider]
    if provider not in JEV_MODELS or not MODEL_ID_RE.fullmatch(model):
        raise ValueError("Jev model: letters, numbers and . _ : / + - only.")
    PREFS.setObject_forKey_(model, f"jev_model_{provider}")


def fish_model():
    return _choice("fish_model", FISH_MODELS, FISH_MODELS[0])


def save_fish_model(model):
    if model not in FISH_MODELS:
        raise ValueError(f"Unknown Fish Audio model: {model}")
    PREFS.setObject_forKey_(model, "fish_model")


def whisper_model():
    return _choice("whisper_model", WHISPER_SIZES, "small.en")


def save_whisper_model(size):
    if size not in WHISPER_SIZES:
        raise ValueError(f"Unknown Whisper size: {size}")
    PREFS.setObject_forKey_(size, "whisper_model")


def ocr_level():
    """Screen text reading: "accurate" (default) or "fast" (quicker, rougher)."""
    return _choice("ocr_level", OCR_LEVELS, "accurate")


def save_ocr_level(level):
    if level not in OCR_LEVELS:
        raise ValueError(f"Unknown text reading mode: {level}")
    PREFS.setObject_forKey_(level, "ocr_level")
