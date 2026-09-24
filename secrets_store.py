"""Store API keys in the macOS login Keychain, with .env as a dev fallback."""
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()  # so a .env works from the app bundle too, not just the terminal


SERVICE = "com.jevsiri.keys"
KEY_NAMES = ("TYPESAFE_API_KEY", "JEV_OPENROUTER_API_KEY", "FISH_AUDIO_API_KEY", "OPENROUTER_API_KEY")
SETTINGS = {"JEV_PROVIDER": ("openrouter", "typesafe"), "ANSWER_PROVIDER": ("disabled", "openrouter")}


def keychain_value(name):
    result = subprocess.run(
        ["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_secret(name):
    return os.getenv(name) or keychain_value(name)  # .env wins, so editing it always takes effect


def save_secret(name, value):
    if name not in KEY_NAMES and name not in SETTINGS:
        raise ValueError(f"unknown secret: {name}")
    value = value.strip()
    if not value:
        return
    if name in SETTINGS and value not in SETTINGS[name]:
        raise ValueError(f"invalid {name}: {value}")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", name, "-s", SERVICE, "-w", value],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not save to Keychain")


def get_setting(name):
    if name not in SETTINGS:
        raise ValueError(f"unknown setting: {name}")
    value = get_secret(name)
    if value:
        if value not in SETTINGS[name]:
            raise ValueError(f"invalid {name}: {value}")
        return value
    if name == "JEV_PROVIDER":
        return "typesafe" if get_secret("TYPESAFE_API_KEY") else "openrouter"
    return "openrouter" if get_secret("OPENROUTER_API_KEY") else "disabled"


def missing_secrets(jev_provider=None, answer_provider=None):
    jev_provider = jev_provider or get_setting("JEV_PROVIDER")
    answer_provider = answer_provider or get_setting("ANSWER_PROVIDER")
    required = ["FISH_AUDIO_API_KEY", "TYPESAFE_API_KEY" if jev_provider == "typesafe" else "JEV_OPENROUTER_API_KEY"]
    if answer_provider == "openrouter":
        required.append("OPENROUTER_API_KEY")
    return [name for name in required if not get_secret(name)]
