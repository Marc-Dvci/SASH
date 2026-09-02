"""Lambda entry point named by ``infra/template.yaml``. See :mod:`sash.aws.handlers`."""

from .handlers import poller as handler

__all__ = ["handler"]
