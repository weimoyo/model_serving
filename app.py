"""ASGI entry point for the standalone model-serving service."""

from model_serving import create_app

app = create_app()

__all__ = ["app"]
