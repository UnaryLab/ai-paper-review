"""Map review categories and sub-ratings to AI personas.

Two small helpers used by the calibration step:

* :func:`route_category` — given a comment's ``category`` string plus a
  ``category_to_persona`` map (from the reviewer DB's validation
  attribution tables), return the persona that should have caught it
  (``None`` if uncategorized).
* :func:`is_low_sub_rating` — threshold an OpenReview-style sub-rating
  on its declared scale.
"""
from __future__ import annotations

from typing import Dict, Optional


def route_category(
    category: str,
    category_to_persona: Dict[str, str],
) -> Optional[str]:
    """Map an actual-review category to the AI persona that should have
    caught it. ``category_to_persona`` comes from the reviewer DB's
    ``Validation Attribution Tables`` block; passing an empty dict
    disables routing (every comment becomes uncategorized)."""
    if not category or not category_to_persona:
        return None
    c = category.strip().lower()
    if c in category_to_persona:
        return category_to_persona[c]
    # Fuzzy match: any key appearing in the category string
    for key, persona in category_to_persona.items():
        if key in c:
            return persona
    return None


def is_low_sub_rating(value: int, scale: int) -> bool:
    """True if the sub-rating counts as 'low' given its scale.

    Scale 3: low = 1
    Scale 4: low = 1 or 2 (OpenReview: 'poor' or 'fair')
    Scale 5: low = 1 or 2
    Scale 10: low = 1..4
    """
    if scale is None or scale <= 0:
        return False
    # "low" = bottom half minus one (strict), or minimum 1
    threshold = max(1, scale // 2) if scale <= 5 else 4
    return value <= threshold
