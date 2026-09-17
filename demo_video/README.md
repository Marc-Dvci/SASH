# The demo video

Under three minutes, narrated, and a recording of the product.

```bash
pip install -e ".[video]"
python -m playwright install chromium
python demo_video/narrate.py     # voice + timing.json + subtitles
python demo_video/record.py      # starts SASH, records, muxes
```

Output: `demo_video/sash-demo.mp4`, plus `demo_subtitles.srt` carrying the same
sentences word for word.

## How it fits together

The walkthrough lives **in the product**, at `src/sash/web/demo.js`, and runs at
<http://127.0.0.1:8000/?demo=1>. Each beat carries the sentence it is about and
then clicks the product's own buttons: the tabs, **Advance**, **Publish this
plan**, **The household follows it**. Every number that appears on screen came
out of `/api/*`.

`narrate.py` reads those sentences out of `demo.js` so there is one copy of the
script, speaks each one with Edge TTS, and **measures** how long each takes.
That measurement is the beat's duration. Sizing a beat by its measured narration keeps the
spotlight moving with the voice.

`record.py` injects those durations as `window.__SASH_TIMING` before any page
script runs, so the visuals pace themselves to the voice from the first beat
rather than catching up to it. Then Playwright records, and ffmpeg fades both
ends and encodes H.264/AAC.

## The two guards

`narrate.py` **fails** if the narration exceeds 176 seconds, and `record.py`
fails if the finished file exceeds 180. Judges are not required to watch past
three minutes. To shorten, edit a sentence in `demo.js` and run `narrate.py`
again, keeping the voice at its natural pace.

## Re-recording

`record.py` starts its own SASH server. Because the walkthrough presses **The
household follows it**, the tape is rewritten, and `sash demo` rebuilds it on the
next start, so every recording begins from the same 120 days and produces the
same numbers. The first run therefore spends a few minutes seeding.

To record against a server you already have running:

```bash
python demo_video/record.py http://127.0.0.1:8000/
```
