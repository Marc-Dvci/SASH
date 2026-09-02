"""Lambda entry point for the product half. See :mod:`sash.aws.handlers`.

Mangum is constructed at import time so the FastAPI app and the cached
household view survive between invocations on a warm container.
"""

from .handlers import api

handler = api()

__all__ = ["handler"]
