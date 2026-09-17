"""One still per beat, out of the finished video, to read against the script.

A recording is judged sentence by sentence: what is on screen while a given
sentence is spoken. This takes ``sash-demo.mp4`` and ``timing.json`` and writes
one PNG per beat into ``frames/``, taken once the beat has settled, plus a
second one near its end for the beats that wait on the product (an Advance, a
Publish). Open them next to ``docs/DEMO_SCRIPT.md``; each picture should show
the thing its sentence is about, lit, with nothing left over from the beat
before.

    python demo_video/frames.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "frames"


def still(video: pathlib.Path, at: float, path: pathlib.Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video),
         "-frames:v", "1", str(path)],
        check=True,
    )


def main() -> int:
    timing = json.loads((HERE / "timing.json").read_text(encoding="utf-8"))
    video = HERE / "sash-demo.mp4"
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    start = timing["lead"]
    for i, seconds in enumerate(timing["beats"], start=1):
        # Settled: after the scroll, the glide and any click have landed.
        still(video, start + min(3.2, seconds * 0.6), OUT / f"beat{i:02d}.png")
        # Late: what the beat looks like once the product has caught up.
        if seconds > 7:
            still(video, start + seconds - 0.8, OUT / f"beat{i:02d}_late.png")
        print(f"beat {i:2d}  {start:6.1f}s  {seconds:5.1f}s")
        start += seconds
    still(video, start + 1.5, OUT / "closing.png")
    print(f"\n{len(list(OUT.glob('*.png')))} stills -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
