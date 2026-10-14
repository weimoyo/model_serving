"""Standalone model-serving control plane and OpenAI gateway."""

from .api import create_app

__all__ = ["create_app"]
