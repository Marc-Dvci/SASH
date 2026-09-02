"""Runtime configuration for SASH.

Everything is read from the environment so the same code runs against the real
Ring Partner API or against the bundled simulator (``simulator/ring_sim.py``)
with nothing but a change of ``RING_API_BASE_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Does not overwrite variables already in os.environ."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_ROOT / ".env")


@dataclass
class RingConfig:
    client_id: str = field(default_factory=lambda: os.environ.get("RING_CLIENT_ID", ""))
    client_secret: str = field(
        default_factory=lambda: os.environ.get("RING_CLIENT_SECRET", "")
    )
    hmac_key: str = field(
        default_factory=lambda: os.environ.get("RING_HMAC_SIGNING_KEY", "sash-dev-key")
    )
    api_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "RING_API_BASE_URL", "https://api.amazonvision.com"
        ).rstrip("/")
    )
    oauth_token_url: str = field(
        default_factory=lambda: os.environ.get(
            "RING_OAUTH_TOKEN_URL", "https://oauth.ring.com/oauth/token"
        )
    )
    oauth_authorize_url: str = field(
        default_factory=lambda: os.environ.get(
            "RING_OAUTH_AUTHORIZE_URL",
            "https://account.ring.com/account/integrations/partner-link/authorize",
        )
    )
    redirect_uri: str = field(
        default_factory=lambda: os.environ.get("RING_REDIRECT_URI", "")
    )

    @property
    def is_simulated(self) -> bool:
        """True when pointed at anything other than the production Ring host."""
        return "amazonvision.com" not in self.api_base_url


@dataclass
class SashConfig:
    ring: RingConfig = field(default_factory=RingConfig)
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("SASH_DB", str(REPO_ROOT / "sash.sqlite")))
    )
    narrative_provider: str = field(
        default_factory=lambda: os.environ.get("SASH_NARRATIVE_PROVIDER", "deterministic")
    )
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "eu-west-1"))
    bedrock_model_id: str = field(
        default_factory=lambda: os.environ.get("BEDROCK_MODEL_ID", "")
    )


# --- Domain constants -------------------------------------------------------
#
# Sourced from Ring product and developer documentation; see docs/RING_API_NOTES.md.

#: The Air Quality Monitor reports temperature, humidity, PM2.5 and CO every 15
#: minutes. Everything downstream is designed around that cadence rather than
#: around a continuous stream, which the API does not provide.
AQM_REPORT_INTERVAL_MIN = 15

#: A PM2.5 sample is usable as the endpoint of an interval only if it is at most
#: this old relative to the time we fetched it.
AQM_MAX_SAMPLE_AGE_MIN = 25

#: Ring's own product-level alert thresholds.
PM25_ALERT_UG_M3 = 35.0
CO_ALERT_PPM = 9.0

#: An analysis interval is the gap between two consecutive fresh PM2.5 samples.
#: Outside this band the pair is discarded (a missed report, or a clock jump).
INTERVAL_MIN_MINUTES = 8.0
INTERVAL_MAX_MINUTES = 40.0

#: Minimum number of usable open-window intervals before a per-opening estimate
#: is shown to the user at all.
MIN_INTERVALS_PER_OPENING = 12

#: Vehicle-motion events closer together than this are treated as one burst for
#: the secondary burst metric. Neither metric is a vehicle count.
BURST_MERGE_SECONDS = 8.0

CONFIG = SashConfig()
