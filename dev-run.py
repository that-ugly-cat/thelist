"""
Run the app locally, reading .env by hand.

In production the environment comes from Docker's `env_file`, so the app itself
has no dotenv dependency and no code path that reads a file — this script is the
only thing that does, and only outside the container.

    python dev-run.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
os.chdir(HERE)

env_path = HERE / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

if not os.environ.get("JWT_SECRET"):
    sys.exit("JWT_SECRET is missing: copy .env.example to .env first.")

import uvicorn  # noqa: E402

uvicorn.run("main:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8020)),
            reload="--reload" in sys.argv)
