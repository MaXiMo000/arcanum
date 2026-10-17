"""Run the API server: python -m arcanum.server (configured through ARCANUM_* environment variables)."""

import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("ARCANUM_HOST", "127.0.0.1"),
                port=int(os.environ.get("ARCANUM_PORT", "8000")))
