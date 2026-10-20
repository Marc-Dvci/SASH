# Demo script

The narration and the beats, as shipped. The authoritative copy is
`src/sash/web/demo.js`: each beat there carries its own sentence, `narrate.py`
reads them out of that file, and `demo_video/demo_subtitles.srt` carries them
again word for word. This document exists so the script can be read without
reading JavaScript.

Total: 18 beats, 172 seconds of narration. The limit is 180 and both scripts fail
rather than exceed it.

---

## The shape

Five movements, in the order a judge's attention is worth spending.

1. **Beats 1-5, the decision (41s).** What SASH says and what it is compared
   against. The product's whole value is on one card, so it is on screen inside
   fifteen seconds.
2. **Beats 6-8, the measurement (35s).** Why this is possible at all: the carbon
   monoxide tracer, and the refusal to over-claim. This is the part a judge has
   not seen before.
3. **Beats 9-13, the loop (46s).** Ring delivering live, a plan published, the
   household acting on it, and the plan scored against readings it produced.
4. **Beat 14, the evidence (15s).** The benchmark numbers.
5. **Beats 15-18, a new household (34s).** Filmed on a second server, a household
   nine days after linking, run forward through its own tape to just after it
   cooked: SASH steps aside and says air now, uses the airing to measure a
   window, and shows each window's progress. Then the camera SASH never looks
   through.

---

## The beats

| # | On screen | Narration |
|---|---|---|
| 1 | Now tab, brand | Two windows in the same room can differ by a factor of five in how much street pollution they let in. Nobody can tell which is which. SASH measures it, from three Ring devices, without ever looking at an image. |
| 2 | The decision headline | This is a real decision, for a flat with three openings. Open the courtyard window, at this time, for this long. |
| 3 | The intake comparison | The street window this household usually reaches for would admit about half as much again, for the same amount of air. |
| 4 | The paired-draw confidence bar | And SASH says how sure it is. Both plans share the same draws, so what is left is the difference between the two windows. |
| 5 | Why now, the demand card | SASH never changes how much air the home gets. Only when, and through which window. |
| 6 | Windows tab, air exchange | Carbon monoxide has no street in it. Between cooking events it decays like a tracer gas, and how fast it decays with each window open gives that window's air exchange rate. |
| 7 | Street coupling | Tracer gas decay normally needs a technician and a cylinder of gas. This household makes the tracer twice a day by cooking, and Ring already measures it. |
| 8 | The verdict card | And SASH refuses to overclaim. Shuffle which window each airing went through, and the courtyard window still wins. The balcony door does not, so SASH says these two are not yet distinguishable. |
| 9 | Timeline, **Advance 30 min** clicked, then the events list | Now watch the events arrive. Signed webhooks through the shipping receiver, and readings through the same client that reads a Ring account. |
| 10 | Now tab, **Publish this plan** clicked, the action row | A recommendation nobody writes down cannot be wrong later. So SASH publishes the plan and its forecast before the airing happens. |
| 11 | **The household follows it**, then **Advance 4 h**, held until it lands | The household opens it, and the flat is simulated again with that window open. |
| 12 | Record tab, this session's plans, the plan scored | Then the plan is scored against the readings that followed. SASH answers only for airings it proposed and that actually happened. |
| 13 | The held-out backtest card | Harder still: fitted on the first part of the record, SASH prices every airing in the last part before seeing its cost. |
| 14 | Windows tab, back to the verdict card | Across twenty-four simulated households it had never seen, SASH beat their own habit on sixty-nine of eighty-eight airings, with a median saving of forty-six per cent. On forty homes with identical windows, it claimed a difference on two. |
| 15 | Second household, nine days after linking, just after cooking: the decision card with **Air now** and **Calibration airing** | Street air is not always the point. Nine days after linking, this household has just cooked. When the reason to open a window is inside the flat, SASH stops optimising for the street, and only picks the window. |
| 16 | The sentence saying the fumes are the measurement | It picks one it has not measured yet, because the fumes fading through it are the measurement. |
| 17 | Getting to know your windows: each window's progress | Each window shows its progress. Guided like this, simulated homes had every window measured in a median of eleven days, instead of forty. |
| 18 | Setup tab, the privacy card | All of it from a camera it never looks through. |

---

## Rules the script follows

**Every number spoken is a number on screen, and every number on screen came out
of the API.** Beats 14 and 17 are the only ones that quote figures from
elsewhere: beat 14's are reproducible with `sash bench --homes 24 --nulls 40
--days 120 --seed 1`, and beat 17's with `python bench/onboarding.py --homes 24
--days 45 --comply 0.6`.

**The demo clicks the product's own buttons.** Beats 9, 10 and 11 press Advance,
Publish, Follow and Advance again, and beat 11 waits on the product's own busy
flag until the four-hour advance has landed, rather than on a fixed sleep. The
recorder gives that beat a hold for the wait and cuts the hold out of the film,
so beat 12 opens on a scored plan however long the advance took. If a button breaks the recording breaks with
it. Every beat is due at a fixed offset from the first, as its sentence is in
the narration, so a press that waits on the product is absorbed by the beat
after it.

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
