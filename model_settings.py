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
