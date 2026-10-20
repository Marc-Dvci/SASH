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

```bash
python demo_video/frames.py      # one still per beat, out of the finished file
```

writes `frames/beatNN.png` for every beat (and `_late` for the beats that wait
on the product). Read them against the table in `docs/DEMO_SCRIPT.md`: each one
should show the thing its sentence is about, lit, with nothing left over from
the beat before. That is the check a viewer performs, so it is the check to run
before uploading.

## How it fits together

The walkthrough lives **in the product**, at `src/sash/web/demo.js`, and runs at
<http://127.0.0.1:8000/?demo=1>. Each beat carries the sentence it is about and
then clicks the product's own buttons: the tabs, **Advance**, **Publish this
plan**, **The household follows it**. Every number that appears on screen came
out of `/api/*`.

`narrate.py` reads those sentences out of `demo.js` so there is one copy of the
script, and **measures** how long each one takes when spoken.
That measurement is the beat's duration. Sizing a beat by its measured narration keeps the
spotlight moving with the voice.

`record.py` injects those durations as `window.__SASH_TIMING` before any page
script runs, so the visuals pace themselves to the voice from the first beat
rather than catching up to it. Then Playwright records, and ffmpeg fades both
ends and encodes H.264/AAC.

The walkthrough has two parts. The first runs on the 120-day household at
`/?demo=1`. The second runs at `/?demo=early` on a household nine days after
linking (`sash demo --days 9`), whose tape `record.py` first advances, half an
hour at a time through the same Advance endpoint, until SASH asks for a
calibration airing: the household has just cooked and a window is still
unmeasured. Each part is filmed against its own server and the two are joined
under the one narration track.

The page is laid out and captured at 1600x900 and scaled to 1920x1080 in the
encode, so YouTube serves it as 1080p. Playwright's own recorder writes a
keyframe every 128 frames at a visibly lower quality than its neighbours, one
soft frame every five seconds; `record.py` finds that cadence from the
capture's sharpness and replaces each such frame with the one before it, so
the clock does not move and the flicker is gone.

## How the picture stays still

The walkthrough scrolls only as far as it takes to bring the thing being
described into the band between the sticky header and the caption, and not at
all when it is already there. The ring of light is clipped to that band, its
label sits on its own plate, the cursor appears only to press a button, and
switching tabs clears whatever was lit before. Beats that press a button light
the button; beats that wait on the product (Advance, Publish) light the card
that will change and measure it again once it has.

## The one cut

**Advance 4 h** takes as long as the machine needs, which on a busy one is
longer than the sentence that presses it. `record.py` gives that beat a hold
(`HOLDS`), the walkthrough waits on the product's busy flag until the advance
lands, and the encode cuts the hold back out, so the narration never waits and
beat 12 always opens on a scored plan. The recorder fails if the advance had
not landed when the cut ends.

## The two guards

`narrate.py` **fails** if the narration exceeds 176 seconds, and `record.py`
fails if the finished file exceeds 180. Judges are not required to watch past
three minutes. To shorten, edit a sentence in `demo.js` and run `narrate.py`
again, keeping the voice at its natural pace.

## Re-recording

`record.py` starts its own SASH servers. Because the walkthrough presses **The
household follows it**, the tape is rewritten, and `sash demo` rebuilds it on the
next start, so every recording begins from the same 120 days and produces the
same numbers. The first run therefore spends a few minutes seeding. The
nine-day household is reseeded on every run, in seconds.
