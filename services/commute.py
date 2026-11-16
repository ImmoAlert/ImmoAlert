"""Commute estimates. Routing is used when configured; otherwise a labelled estimate."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

import requests

from config import settings

MODES = {"walk": "foot", "bike": "bike", "car": "driving", "transit": None}
# (average km/h, detour factor over the straight line, fixed minutes)
_ASSUMPTIONS = {"walk": (4.8, 1.25, 0), "bike": (14.0, 1.25, 0), "car": (32.0, 1.3, 4), "transit": (20.0, 1.3, 8)}


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(h))


def estimate_from_points(origin, destination, mode):
    if mode not in MODES:
        raise ValueError("Mode de trajet inconnu.")
    speed, detour, fixed = _ASSUMPTIONS[mode]
    distance = haversine_km(origin, destination)
    minutes = round(distance * detour / speed * 60 + fixed)
    return {"minutes": max(1, min(600, minutes)), "distance_km": round(distance, 1), "method": "estimate"}


def route_minutes(origin, destination, mode):
    """Ask an OSRM-compatible service. Returns None when it is not usable."""
    profile = MODES.get(mode)
    if not settings.commute_routing_url or not profile:
        return None
    base = settings.commute_routing_url.rstrip("/")
    url = f"{base}/route/v1/{profile}/{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
    try:
        reply = requests.get(url, params={"overview": "false"}, timeout=6)
        reply.raise_for_status()
        route = reply.json()["routes"][0]
        return {"minutes": max(1, min(600, round(route["duration"] / 60))), "distance_km": round(route["distance"] / 1000, 1), "method": "route"}
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None


def estimate(listing, destination, mode):
    """Estimate the time between a listing's known position and a destination."""
    from utils.geo import geocode
    if not isinstance(destination, str) or not 2 <= len(destination.strip()) <= 160:
        raise ValueError("Indiquez une destination de 2 à 160 caractères.")
    if mode not in MODES:
        raise ValueError("Mode de trajet inconnu.")
    if listing.latitude is not None and listing.longitude is not None:
        origin = (listing.latitude, listing.longitude)
        precise = listing.geo_precision == "portal"
    else:
        point = geocode(listing.location or "")
        if point is None:
            raise ValueError("La position du bien n’est pas assez précise pour estimer un trajet.")
        origin, precise = (point.lat, point.lon), False
    target = geocode(destination.strip())
    if target is None:
        raise ValueError("Destination introuvable. Précisez l’adresse ou la ville.")
    result = route_minutes(origin, (target.lat, target.lon), mode) or estimate_from_points(origin, (target.lat, target.lon), mode)
    result["approximate_origin"] = not precise
    result["detail"] = ("Trajet calculé par un service d’itinéraire." if result["method"] == "route" else
        "Estimation à vol d’oiseau avec un détour moyen ; elle ne remplace pas un itinéraire réel.") + (
        "" if precise else " La position du bien est indicative.")
    return result
