"""Entry point: `python -m jarvis` or `jarvis`."""

from __future__ import annotations

import logging

from jarvis.api.server import run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
