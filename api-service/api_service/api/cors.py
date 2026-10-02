"""CORS for browser frontends (CORS_ORIGINS). Off when the list is empty."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from telemetry_shared.config import get_settings


def add_cors(app: FastAPI) -> None:
    """Add last, so it wraps ApiTokenMiddleware: preflight OPTIONS requests are answered before the token check."""
    origins = get_settings().cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
        )
