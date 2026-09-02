"""Turning a fitted model into a sentence a household would act on.

Every sentence SASH shows is built here from numbers that are already in the
model. The default renderer is deterministic: the same state produces the same
words, so a screenshot can be checked against the data behind it and a
regression in the phrasing is a test failure rather than a matter of taste.

An optional adapter sends the same structured facts to Amazon Bedrock for a
warmer phrasing. It is off unless ``BEDROCK_MODEL_ID`` is set, it is given the
numbers rather than the raw history, and if it fails or is slow the
deterministic text is what ships. A ventilation adviser that cannot speak when
the network is down is not an adviser.

The rule the adapter enforces, and the reason the fact dictionary exists: the
model never invents a quantity. It is handed the numbers and asked only to join
them up.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Any

BEDROCK_SYSTEM = (
    "You write one short paragraph for a home ventilation adviser. "
    "You are given a JSON object of facts that were measured. "
    "Use only those numbers. Never invent a quantity, a device, or a health "
    "claim. Two sentences at most. No exclamation marks. British English."
)


@dataclass
class Facts:
    """Everything a sentence is allowed to draw on."""

    opening: str
    start_local: str
    duration_min: float
    predicted_intake: float
    interval: tuple[float, float]
    habit_opening: str | None
    habit_intake: float | None
    saving_pct: float | None
    p_better: float | None
    activity_band: str
    drivers: list[str]
    exploration: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "opening": self.opening,
            "start_local": self.start_local,
            "duration_min": round(self.duration_min),
            "predicted_intake_ug_min_per_m3": round(self.predicted_intake),
            "interval": [round(v) for v in self.interval],
            "habit_opening": self.habit_opening,
            "habit_intake_ug_min_per_m3": (
                None if self.habit_intake is None else round(self.habit_intake)
            ),
            "saving_pct": None if self.saving_pct is None else round(self.saving_pct),
            "share_of_paired_draws_cheaper": (
                None if self.p_better is None else round(self.p_better, 2)
            ),
            "street_activity_band": self.activity_band,
            "why_now": self.drivers,
            "is_exploration": self.exploration,
        }


def facts_from(recommendation: dict[str, Any], tz_offset_h: float = 0.0) -> Facts | None:
    best = recommendation.get("best")
    if not best:
        return None
    habit = recommendation.get("habit")
    stamp = dt.datetime.fromtimestamp(
        best["start_at"] + tz_offset_h * 3600.0, tz=dt.timezone.utc
    )
    return Facts(
        opening=best["opening_label"],
        start_local=stamp.strftime("%H:%M"),
        duration_min=best["duration_min"],
        predicted_intake=best["pred_intake"],
        interval=(best["pred_intake_lo"], best["pred_intake_hi"]),
        habit_opening=habit["opening_label"] if habit else None,
        habit_intake=habit["pred_intake"] if habit else None,
        saving_pct=recommendation.get("saving_pct"),
        p_better=recommendation.get("p_better"),
        activity_band=best["activity_band"],
        drivers=recommendation.get("demand", {}).get("drivers", []),
        exploration=bool(best.get("exploration")),
    )


def render(facts: Facts) -> str:
    """The deterministic phrasing. This is what ships."""
    if facts.activity_band in ("high", "very high"):
        street = (
            f"even though the street is {facts.activity_band} for this time of "
            "week, because this opening barely reacts to it"
        )
    else:
        street = f"while the street is {facts.activity_band} for this time of week"
    lead = (
        f"Open the {facts.opening} at {facts.start_local} for "
        f"{facts.duration_min:.0f} minutes, {street}."
    )
    cost = (
        f"That should let in about {facts.predicted_intake:.0f} "
        "microgram-minutes per cubic metre above a closed home."
    )
    parts = [lead, cost]
    if facts.exploration:
        parts.append(
            "This one is a deliberate experiment: SASH has not seen this opening "
            "often enough to compare it, and the only way to find out is to use it."
        )
    elif (
        facts.habit_opening
        and facts.habit_opening != facts.opening
        and facts.saving_pct is not None
        and facts.saving_pct > 0
    ):
        sentence = (
            f"The {facts.habit_opening}, which this home usually reaches for, "
            f"would let in about {facts.habit_intake:.0f}, so this is "
            f"{facts.saving_pct:.0f} per cent less for the same amount of air"
        )
        if facts.p_better is not None:
            sentence += (
                f", and it is the cheaper of the two in "
                f"{facts.p_better * 100:.0f} per cent of paired draws"
            )
        parts.append(sentence + ".")
    if facts.drivers:
        parts.append("Airing is due because " + "; ".join(facts.drivers) + ".")
    return " ".join(parts)


class BedrockNarrator:
    """Optional warmer phrasing through Amazon Bedrock.

    Constructed lazily and never on the request path without a timeout. If
    anything at all goes wrong the deterministic renderer answers instead, and
    the response says which one spoke.
    """

    def __init__(self, model_id: str, region: str, timeout_s: float = 4.0):
        self.model_id = model_id
        self.region = region
        self.timeout_s = timeout_s
        self._client = None

    def _connect(self):
        if self._client is None:
            import boto3  # imported here so boto3 stays an optional dependency
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    connect_timeout=self.timeout_s,
                    read_timeout=self.timeout_s,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    def speak(self, facts: Facts) -> str:
        client = self._connect()
        response = client.converse(
            modelId=self.model_id,
            system=[{"text": BEDROCK_SYSTEM}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": json.dumps(facts.as_dict(), indent=2)}],
                }
            ],
            inferenceConfig={"maxTokens": 220, "temperature": 0.2},
        )
        return response["output"]["message"]["content"][0]["text"].strip()


def narrate(
    recommendation: dict[str, Any],
    *,
    provider: str = "deterministic",
    model_id: str = "",
    region: str = "eu-west-1",
    tz_offset_h: float = 0.0,
) -> dict[str, Any]:
    """Return ``{text, source, facts}``.

    ``source`` is part of the payload because a household reading advice about
    its own air is entitled to know whether a language model wrote the sentence.
    """
    facts = facts_from(recommendation, tz_offset_h=tz_offset_h)
    if facts is None:
        return {
            "text": recommendation.get("reason", "SASH has nothing to recommend yet."),
            "source": "deterministic",
            "facts": None,
        }

    fallback = render(facts)
    if provider != "bedrock" or not model_id:
        return {"text": fallback, "source": "deterministic", "facts": facts.as_dict()}

    try:
        text = BedrockNarrator(model_id, region).speak(facts)
        if not text:
            raise ValueError("empty completion")
        return {"text": text, "source": f"bedrock:{model_id}", "facts": facts.as_dict()}
    except Exception as exc:  # noqa: BLE001 - any failure falls back, by design
        return {
            "text": fallback,
            "source": "deterministic",
            "facts": facts.as_dict(),
            "fallback_reason": f"{type(exc).__name__}: {exc}"[:200],
        }


def provider_from_env() -> tuple[str, str, str]:
    return (
        os.environ.get("SASH_NARRATIVE_PROVIDER", "deterministic"),
        os.environ.get("BEDROCK_MODEL_ID", ""),
        os.environ.get("AWS_REGION", "eu-west-1"),
    )
