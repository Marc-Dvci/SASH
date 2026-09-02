"""Lambda entry point named by ``infra/template.yaml``. See :mod:`sash.aws.handlers`."""

from .handlers import processor as handler

__all__ = ["handler"]
