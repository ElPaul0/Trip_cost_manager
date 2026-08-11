"""Client HERE Maps : Autosuggest + Routing (distance + péages)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


AUTOSUGGEST_URL = "https://autosuggest.search.hereapi.com/v1/autosuggest"
ROUTING_URL = "https://router.hereapi.com/v8/routes"

# Centre approximatif de la France (biais de recherche)
DEFAULT_AT = "46.603354,1.888334"


class HereMapsError(Exception):
    """Erreur métier ou réseau liée à HERE."""


@dataclass(frozen=True)
class PlaceSuggestion:
    label: str
    lat: float
    lng: float


@dataclass(frozen=True)
class RouteEstimate:
    distance_km: float
    tolls_eur: float
    duration_seconds: int


def get_api_key() -> str | None:
    key = os.getenv("HERE_API_KEY", "").strip()
    return key or None


def is_enabled() -> bool:
    if os.getenv("HERE_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        return False
    return get_api_key() is not None


def _request_json(url: str, timeout: float = 12.0) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise HereMapsError(f"HERE HTTP {exc.code} : {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise HereMapsError(f"Impossible de joindre HERE : {exc.reason}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HereMapsError("Réponse HERE invalide.") from exc


def suggest_places(query: str, *, limit: int = 6) -> list[PlaceSuggestion]:
    api_key = get_api_key()
    if not api_key:
        raise HereMapsError("HERE_API_KEY non configurée.")

    q = query.strip()
    if len(q) < 2:
        return []

    params = urllib.parse.urlencode(
        {
            "q": q,
            "at": DEFAULT_AT,
            "lang": "fr-FR",
            "limit": max(1, min(limit, 10)),
            "apiKey": api_key,
        }
    )
    data = _request_json(f"{AUTOSUGGEST_URL}?{params}")
    if not isinstance(data, dict):
        return []

    suggestions: list[PlaceSuggestion] = []
    for item in data.get("items") or []:
        position = item.get("position") or {}
        lat = position.get("lat")
        lng = position.get("lng")
        if lat is None or lng is None:
            continue
        address = item.get("address") or {}
        label = (
            address.get("label")
            or item.get("title")
            or f"{lat},{lng}"
        )
        suggestions.append(
            PlaceSuggestion(label=str(label), lat=float(lat), lng=float(lng))
        )
    return suggestions


def _extract_tolls_eur(route: dict) -> float:
    total = 0.0
    for section in route.get("sections") or []:
        for toll in section.get("tolls") or []:
            for fare in toll.get("fares") or []:
                price = fare.get("convertedPrice") or fare.get("price") or {}
                if price.get("type") == "value" and price.get("value") is not None:
                    try:
                        total += float(price["value"])
                    except (TypeError, ValueError):
                        continue
    return round(total, 2)


def calculate_route(
    *,
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
) -> RouteEstimate:
    api_key = get_api_key()
    if not api_key:
        raise HereMapsError("HERE_API_KEY non configurée.")

    params = urllib.parse.urlencode(
        {
            "transportMode": "car",
            "origin": f"{origin_lat},{origin_lng}",
            "destination": f"{destination_lat},{destination_lng}",
            "return": "summary,tolls",
            "currency": "EUR",
            "apiKey": api_key,
        }
    )
    data = _request_json(f"{ROUTING_URL}?{params}")
    if not isinstance(data, dict):
        raise HereMapsError("Réponse d’itinéraire invalide.")

    routes = data.get("routes") or []
    if not routes:
        notices = data.get("notices") or []
        if notices:
            raise HereMapsError("Aucun itinéraire trouvé pour ces lieux.")
        raise HereMapsError("Aucun itinéraire trouvé.")

    route = routes[0]
    length_m = 0
    duration_s = 0
    for section in route.get("sections") or []:
        summary = section.get("summary") or {}
        length_m += int(summary.get("length") or 0)
        duration_s += int(summary.get("duration") or 0)

    if length_m <= 0:
        raise HereMapsError("Distance d’itinéraire invalide.")

    return RouteEstimate(
        distance_km=round(length_m / 1000.0, 1),
        tolls_eur=_extract_tolls_eur(route),
        duration_seconds=duration_s,
    )
