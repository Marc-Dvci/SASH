"""Speak the walkthrough, and measure how long each beat actually takes.

The product ships a guided walkthrough (``window.SashDemo``, at ``/?demo=1``)
whose beats each carry the sentence they are about. This script reads those
sentences out of the product itself, speaks each one with Edge TTS, and produces
two artifacts:

  * ``narration.wav`` -- one 48 kHz master with each beat's speech at its cue.
  * ``timing.json``   -- the measured duration of each beat. ``record.py``
    injects it as ``window.__SASH_TIMING`` so the visuals pace themselves to the
    voice and both share one clock.

The beat durations are **measured from the synthesised audio**, never guessed.
A beat sized by a guess either cuts the sentence off or leaves the spotlight
sitting on a card in silence, and both look like a bug.

    python demo_video/narrate.py        # this first
    python demo_video/record.py         # then this
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import wave

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

VOICE = "en-GB-RyanNeural"
RATE = "+6%"

#: Silence before the first word, so the page has painted.
LEAD_SILENCE = 0.6
#: Silence after each beat, so a sentence does not run into the next spotlight.
GAP_SECONDS = 0.45
#: Devpost: judges are not required to watch past three minutes.
HARD_LIMIT_SECONDS = 176.0

SPEECH_DIR = HERE / "speech"


def beats() -> list[dict]:
    """The beat list, read out of ``web/demo.js`` so there is one copy of it."""
    source = (ROOT / "src" / "sash" / "web" / "demo.js").read_text(encoding="utf-8")
    import re

    out = []
    for match in re.finditer(r"say:\s*((?:\"[^\"]*\"\s*\+?\s*)+),\s*\n\s*seconds:\s*([\d.]+)",
                             source):
        text = "".join(re.findall(r'"([^"]*)"', match.group(1)))
        out.append({"say": text.strip(), "seconds": float(match.group(2))})
    if not out:
        raise SystemExit("no beats found in web/demo.js")
    return out


async def speak(text: str, path: pathlib.Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
    await communicate.save(str(path))


def duration_of(path: pathlib.Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def to_wav(src: pathlib.Path, dst: pathlib.Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "48000", "-ac", "1", str(dst)],
        check=True,
    )


def main() -> int:
    SPEECH_DIR.mkdir(exist_ok=True)
    plan = beats()
    print(f"{len(plan)} beats")

    timings: list[float] = []
    wavs: list[pathlib.Path] = []
    for i, beat in enumerate(plan):
        mp3 = SPEECH_DIR / f"{i:02d}.mp3"
        wav = SPEECH_DIR / f"{i:02d}.wav"
        asyncio.run(speak(beat["say"], mp3))
        to_wav(mp3, wav)
        spoken = duration_of(wav)
        # The beat lasts as long as the sentence takes, plus a breath. The
        # number in demo.js is only the fallback for a live viewing.
        timings.append(round(spoken + GAP_SECONDS, 3))
        wavs.append(wav)
        print(f"  [{i:02d}] {spoken:5.2f}s  {beat['say'][:66]}...")

    total = LEAD_SILENCE + sum(timings)
    print(f"\ntotal {total:.1f}s")
    if total > HARD_LIMIT_SECONDS:
        print(
            f"OVER THE LIMIT by {total - HARD_LIMIT_SECONDS:.1f}s. "
            "Shorten a sentence in web/demo.js and run this again; do not speed "
            "the voice up, a rushed narration reads as a rushed product.",
            file=sys.stderr,
        )
        return 1

    # One master, each beat placed at its cue.
    master = HERE / "narration.wav"
    frames: list[bytes] = []
    rate = 48000
    with wave.open(str(wavs[0])) as w:
        params = w.getparams()
    frames.append(b"\x00\x00" * int(LEAD_SILENCE * rate))
    for wav, allotted in zip(wavs, timings):
        with wave.open(str(wav)) as w:
            data = w.readframes(w.getnframes())
        frames.append(data)
        pad = int(allotted * rate) - len(data) // 2
        if pad > 0:
            frames.append(b"\x00\x00" * pad)
    with wave.open(str(master), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(params.sampwidth)
        out.setframerate(rate)
        out.writeframes(b"".join(frames))

    (HERE / "timing.json").write_text(
        json.dumps({"lead": LEAD_SILENCE, "beats": timings, "total": round(total, 3)},
                   indent=1),
        encoding="utf-8",
    )

    # Subtitles, word for word the same text the product burns in as captions.
    cues = []
    t = LEAD_SILENCE
    for i, (beat, allotted) in enumerate(zip(plan, timings), start=1):
        cues.append(f"{i}\n{_stamp(t)} --> {_stamp(t + allotted)}\n{beat['say']}\n")
        t += allotted
    (HERE / "demo_subtitles.srt").write_text("\n".join(cues), encoding="utf-8")

    print(f"wrote {master.name}, timing.json, demo_subtitles.srt")
    return 0


def _stamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


if __name__ == "__main__":
    raise SystemExit(main())
