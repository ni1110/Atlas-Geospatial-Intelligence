"""Deprecated import path for the canonical asynchronous FastAPI application."""

from renewable_site_agent.api import app, main

__all__ = ["app", "main"]


if __name__ == "__main__":
    main()
