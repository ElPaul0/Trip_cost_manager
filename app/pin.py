"""Code PIN à 4 chiffres optionnel pour protéger un utilisateur."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets


def normalize_pin(pin: str | None) -> str:
    return (pin or "").strip()


def validate_pin_format(pin: str | None) -> str | None:
    """Retourne un message d’erreur, ou None si le PIN est valide (vide inclus)."""
    value = normalize_pin(pin)
    if value == "":
        return None
    if not re.fullmatch(r"\d{4}", value):
        return "Le code doit contenir exactement 4 chiffres, ou être laissé vide."
    return None


def hash_pin(pin: str | None) -> str:
    value = normalize_pin(pin)
    if value == "":
        return ""
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_pin(pin: str | None, stored_hash: str | None) -> bool:
    stored = (stored_hash or "").strip()
    if stored == "":
        return True
    value = normalize_pin(pin)
    if value == "" or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    check = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(check, digest)


def user_has_pin(pin_hash: str | None) -> bool:
    return bool((pin_hash or "").strip())
