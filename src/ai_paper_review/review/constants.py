"""Shared constants for the review pipeline."""
from __future__ import annotations

# Below 5 loses the cross-reviewer consensus signal clustering relies
# on; above 10 hits diminishing returns since the 20 personas cap
# sensible diversification.
DEFAULT_N_REVIEWERS = 10
MIN_N_REVIEWERS = 1
MAX_N_REVIEWERS = 20
RECOMMENDED_MIN_N_REVIEWERS = 5
RECOMMENDED_MAX_N_REVIEWERS = 10


SEVERITY_WEIGHT = {
    "major":    3.0,
    "moderate": 2.0,
    "minor":    1.0,
}


# 5 catches the transient "LLM returned a rating with no comments" case
# in practice without runaway cost on persistent failures (each retry is
# a full reviewer call).
EMPTY_COMMENT_RETRIES = 5
