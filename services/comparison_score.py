"""Optional comparison score. Only criteria with data count; nothing is invented."""
from __future__ import annotations


def _relative(value, values, lower_is_better=True):
    """0–100 within the compared set; 100 for the best value."""
    low, high = min(values), max(values)
    if high == low:
        return 100.0
    ratio = (value - low) / (high - low)
    return round(100 * (1 - ratio if lower_is_better else ratio), 1)


def _per_m2(item):
    if isinstance(item.get("price_per_m2"), (int, float)):
        return item["price_per_m2"]
    price, surface = item.get("price"), item.get("surface_m2")
    return price / surface if isinstance(price, (int, float)) and isinstance(surface, (int, float)) and surface > 0 else None


def score_items(items):
    """Add `score` (0–100 or None) and `score_basis` to comparison items in place."""
    prices = [v for v in map(_per_m2, items) if v is not None]
    commutes = [(i.get("personal_criteria") or {}).get("commute_minutes") for i in items]
    commutes = [c for c in commutes if isinstance(c, (int, float))]
    for item in items:
        parts = []
        criteria = item.get("personal_criteria") or {}
        if _per_m2(item) is not None and len(prices) >= 2:
            parts.append(("Loyer au m²", 40, _relative(_per_m2(item), prices)))
        if isinstance(criteria.get("commute_minutes"), (int, float)) and len(commutes) >= 2:
            parts.append(("Trajet", 35, _relative(criteria["commute_minutes"], commutes)))
        for key, label in (("parking", "Stationnement"), ("accessibility", "Accessibilité")):
            if criteria.get(key) in {"yes", "no"}:
                parts.append((label, 12.5, 100.0 if criteria[key] == "yes" else 0.0))
        weight = sum(p[1] for p in parts)
        item["score"] = round(sum(p[1] * p[2] for p in parts) / weight) if len(parts) >= 2 else None
        item["score_basis"] = [p[0] for p in parts]
    return items
