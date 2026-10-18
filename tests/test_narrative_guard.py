"""A sentence from Bedrock ships only when every number in it is a measured one."""

from __future__ import annotations

from sash import narrative

REC = {
    "best": {
        "start_at": 1789058700.0 - 16 * 3600 + 2 * 3600,
        "opening_label": "Courtyard window",
        "duration_min": 57.0,
        "pred_intake": 388.2,
        "pred_intake_lo": 301.0,
        "pred_intake_hi": 470.0,
        "activity_band": "typical",
    },
    "habit": {"opening_label": "Street window", "pred_intake": 597.4},
    "saving_pct": 35.0,
    "p_better": 0.97,
    "demand": {"drivers": ["0.9 of the 3.2 air changes this home usually gets in half a day"]},
}


def _facts():
    return narrative.facts_from(REC)


def test_measured_numbers_pass():
    f = _facts()
    text = (f"Open the courtyard window at {f.start_local} for 57 minutes: about 388, against 597 for "
            "the street window, 35 % less, cheaper in 97 % of paired draws.")
    assert narrative.numbers_supported(text, f)


def test_an_invented_number_is_refused():
    assert not narrative.numbers_supported("This cuts your exposure by 60 per cent.", _facts())


def test_refused_sentence_falls_back_to_the_deterministic_one(monkeypatch):
    narrative._SPOKEN.clear()
    monkeypatch.setattr(narrative.BedrockNarrator, "speak", lambda self, facts: "Open it now for 90 minutes.")
    out = narrative.narrate(REC, provider="bedrock", model_id="m", endpoint="https://example.invalid")
    assert out["source"] == "deterministic"
    assert out["text"] == narrative.render(_facts())
    assert "facts do not hold" in out["fallback_reason"]


def test_accepted_sentence_is_labelled_with_its_model(monkeypatch):
    narrative._SPOKEN.clear()
    monkeypatch.setattr(narrative.BedrockNarrator, "speak", lambda self, facts: "About 388, against 597.")
    out = narrative.narrate(REC, provider="bedrock", model_id="m", endpoint="https://example.invalid")
    assert out["source"] == "bedrock:m"


def test_endpoint_transport_is_chosen_when_an_endpoint_is_set(monkeypatch):
    seen = {}

    def fake(self, facts):
        seen["endpoint"] = self.endpoint
        return "About 388."

    monkeypatch.setattr(narrative.BedrockNarrator, "_speak_endpoint", fake)
    narrator = narrative.BedrockNarrator("m", "us-east-1", endpoint="https://bedrock-mantle.us-east-1.api.aws/")
    assert narrator.speak(_facts()) == "About 388."
    assert seen["endpoint"] == "https://bedrock-mantle.us-east-1.api.aws"
