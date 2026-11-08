import os
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent


def quotes_dir() -> Path:
    return (BACKEND_DIR / os.environ.get("QUOTES_DIR", "../../quotes")).resolve()


@pytest.fixture
def quote_bytes():
    """Return a loader for a quote PDF's bytes; skips when the file is absent."""

    def load(name: str) -> bytes:
        path = quotes_dir() / name
        if not path.exists():
            pytest.skip(f"{path} not available")
        return path.read_bytes()

    return load
