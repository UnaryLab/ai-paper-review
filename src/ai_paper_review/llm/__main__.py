"""``python -m ai_paper_review.llm`` — dump the resolved config to stdout.

Handy for confirming the expected provider/model gets picked when
debugging config.yaml + env-var override interactions.
"""
from __future__ import annotations

import json
import logging

from .probing import describe_config


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(describe_config(), indent=2))


if __name__ == "__main__":
    main()
