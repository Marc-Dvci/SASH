# Demo script

The narration and the beats, as shipped. The authoritative copy is
`src/sash/web/demo.js`: each beat there carries its own sentence, `narrate.py`
reads them out of that file, and `demo_video/demo_subtitles.srt` carries them
again word for word. This document exists so the script can be read without
reading JavaScript.

Total: 16 beats, 174 seconds of narration. The limit is 180 and both scripts fail
rather than exceed it.

---

## The shape

Four movements, in the order a judge's attention is worth spending.

1. **Beats 1-5, the decision (52s).** What SASH says and what it is compared
   against. The product's whole value is on one card, so it is on screen inside
   fifteen seconds.
2. **Beats 6-8, the measurement (36s).** Why this is possible at all: the carbon
   monoxide tracer, and the refusal to over-claim. This is the part a judge has
   not seen before.
3. **Beats 9-13, the loop (45s).** Ring delivering live, a plan published, the
   household acting on it, and the plan scored against readings it produced.
4. **Beats 14-16, the evidence and the privacy (41s).** The benchmark numbers,
   and the camera SASH never looks through.

---

## The beats

| # | On screen | Narration |
|---|---|---|
| 1 | Now tab, brand | Two windows in the same room can differ by a factor of five in how much street pollution they let in. Nobody can tell which is which. SASH measures it, from three Ring devices, without ever looking at an image. |
| 2 | The decision headline | This is a real decision, taken now, for a flat with three openings. Open the courtyard window, at this time, for this long. |
| 3 | The intake comparison | It is compared against what this household actually does. The street window it usually reaches for would admit about half as much again, for the same amount of air. |
| 4 | The paired-draw confidence bar | And SASH says how sure it is. Both plans are forecast together on each bootstrap draw, so they share the same weather and the same day, and what is left is the difference between the two windows. |
| 5 | Why now, the demand card | SASH never changes how much air the home gets. The target is this household's own median. It moves when you air, and which window, and nothing else. |
| 6 | Windows tab, air exchange | Here is the measurement that makes it possible. Carbon monoxide has no street in it. Between cooking events it is a decaying tracer, so regressing its decay rate on how open each window was gives that window's air exchange rate directly. |
| 7 | Street coupling | That is tracer gas decay, which normally needs a technician and a cylinder of gas. This household makes the tracer twice a day by cooking, and Ring already measures it. |
| 8 | The verdict card | And SASH refuses to over-claim. Every airing is held as it happened and only the label is shuffled. The courtyard window survives that. The balcony door does not, so SASH says these two are not yet distinguishable. |
| 9 | Timeline, **Advance 30 min** clicked, then the events list | Now watch the events arrive. Every one is a signed webhook through the shipping receiver, and every reading is fetched by the same client that reads a Ring account. |
| 10 | The last-24-hours card | The readings arrive, the model refits, and the plan moves with them. |
| 11 | Now tab, **Publish this plan** clicked, the action row | A recommendation nobody writes down cannot be wrong later. So SASH publishes the plan, with its forecast and its interval, before the airing happens. |
| 12 | **The household follows it**, then **Advance 4 h** started | The household opens it. The flat is re-integrated with that window open, on the weather and the street that were already drawn, so the readings afterwards are what it would really have measured. |
| 13 | Record tab, this session's plans, the row settling | And it gets scored against the readings that followed. SASH takes the credit and the blame only for airings it proposed and that actually happened. |
| 14 | The held-out backtest card | Above it, the harder version. The model is fitted on the first part of this home's record and asked to price every airing in the last part before seeing what it cost. |
| 15 | Windows tab, back to the verdict card | Across twenty-four simulated households it has never seen, SASH beat the household's own habit on seventy-one of ninety-four airings. On forty homes whose windows are physically identical, it announced a difference on two, at a nominal five per cent. |
| 16 | Setup tab, the privacy card | All of it from a camera it never looks through. The Ring client carries a path allowlist, and the test suite walks every media endpoint against a server that would have answered. |

---

## Rules the script follows

**Every number spoken is a number on screen, and every number on screen came out
of the API.** Beat 15 is the only one that quotes figures from elsewhere, and
those are reproducible with `sash bench --homes 24 --nulls 40 --days 120
--seed 1`.

**The demo clicks the product's own buttons.** Beats 9, 11 and 12 press Advance,
Publish, Follow and Advance again, and beat 13 waits on the product's own busy
flag rather than on a fixed sleep. If a button breaks the recording breaks with
it.

**Every beat lights one thing, and only that thing.** A tab change clears the
previous ring, a press lights the button pressed, and a card that will change
is lit while it changes and measured again after. `demo_video/frames.py` cuts
one still per beat out of the finished file so this can be read off rather
than assumed.

**Beat 4 exists because beat 3 is a claim.** A saving is shown with how sure
SASH is of it.

**Beat 8 is the one to keep if anything has to be cut.** A ventilation adviser
that says "these two windows are not yet distinguishable" is doing something
almost nothing in this category does.

**The narration never says "we".** Solo entry.
