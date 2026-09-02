# Benchmark

The demo home proves SASH runs. It does not prove SASH is right, because it is
one household and its answer is already known.

`sash bench` simulates households SASH has never seen, with different physics and
different habits, hands it nothing but the streams a Ring account would deliver,
and scores five things it is possible to be wrong about. The scoring is against
`simulator/home.py`, which is a single-zone mass balance on a one-minute grid.
SASH never sees any of it.

```bash
sash bench --homes 24 --nulls 40 --days 120 --seed 1
```

Results below are that command, verbatim, from `bench/results/`. It takes about
seven minutes.

---

## 1. Recovery

Does the fit find the numbers the simulator was built with?

| quantity | median error |
|---|---|
| Sealed-home loss rate | 0.033 per hour, absolute |
| Air exchange rate, per opening | **7.1%** relative |
| Street coupling, per opening | 1.06 ug/m3 per event/min, absolute (33% relative) |

62 of 62 openings across 24 households passed the reporting gate. An opening that
does not pass is shown with its own reason ("this opening needs 6 more airings
with the carbon-monoxide channel above its floor"), never as a spinner.

The exchange rate is the number recovered well, and that is the CO tracer doing
its job: it is measured on a channel with no street term in it, so nothing about
the traffic can contaminate it. The coupling is harder, which is why the gate
below exists.

## 2. Ranking, and what the gate is for

SASH named the truly least-coupled opening in **19 of 24** households (79.2%).

That number mixes two different questions, and the product already knows the
difference. `random_home` builds households with two or three openings, and a
three-opening home has one street-facing window and two quiet ones whose true
couplings are often a hair apart. Getting *that* ordering right is a coin toss
nobody should be scored on, and SASH does not try: the permutation gate refuses
to call it.

It refuses on the right ones. Across the 38 pairwise comparisons in those 24
households, the gate **called 15 and withheld 23**, and the median true gap
between the two openings is:

| | median true gap, ug/m3 per event/min |
|---|---|
| Pairs SASH called | **8.40** |
| Pairs SASH withheld | **1.45** |

**Of the 15 pairs it called, the opening it named as cheaper really was cheaper
in 14 (93.3%).**

That is the number the product rests on. SASH is not a system that is right 79%
of the time; it is a system that is right 93% of the time and says so the rest of
the time.

## 3. Decision quality

Not "did the estimate look good" but "was the advice better than what the
household would have done anyway". SASH's recommended airing and the household's
own habitual airing are both re-integrated in the simulator on the record that
actually happened, so the comparison is between two real trajectories of the same
flat on the same day, with the same weather, the same street and the same
cooking. Neither trajectory is one SASH fitted.

| | |
|---|---|
| Recommendations that really cost less than the habit | **71 of 94** (75.5%) |
| Median realised saving | **44.5%** |
| Median forecast saving | 50.7% |
| Restricted to homes whose openings are distinguishable at all | **22 of 27** (81.5%), median realised saving **65.2%** |

The forecast saving slightly over-states the realised one, which is the direction
to expect from a selected maximum and is worth stating rather than rounding away.

## 4. False claims on openings that are identical

`null_home` builds a household whose two openings have **the same** air exchange
rate and **the same** street coupling. There is nothing to find. The household
still favours one of them, still uses them at different times, and the estimates
still come out different, because they are estimates.

**2 of 40** null households had a difference announced. That is **5.0%**, Wilson
95% interval 1.4% to 16.5%, against a permutation gate running at alpha = 0.05
over 400 shuffles.

This is the number a judge should look at first, and it is the reason the
sample is 40 rather than 16. At 16 nulls the observed rate was 12.5%, which
looks alarming and is entirely consistent with 5% at that sample size. Two
candidate mechanisms for a systematic over-rejection were tested and both were
negative: balancing the two openings' airing counts before comparing (the
less-used opening does carry an upward-biased coupling, 12 of 16 nulls, but
correcting it did not move the p-values), and drawing the exchange rates from
their bootstrap inside each permutation. Neither helped, because there was
nothing there to help.

## 5. Pricing one airing

3,517 held-out airings. Each is forecast **before it happened** from a model
fitted on the first half of that home's record, measured **afterwards** from the
readings alone, and both are compared with the simulator's own answer for the
same airing.

| quantity | value |
|---|---|
| Median true cost of an airing | 682 ug/m3 x min |
| Median error of the forecast | **354** (52% of it) |
| Median error of the retrospective measurement | **178** |
| Median bias of that measurement | −39 |
| Coverage of the 90% prediction interval | **88.6%** |

Two things to take from this.

**The interval is honest.** 88.6% of a nominal 90%, on a slice that neither the
fit nor the interval calibration ever saw. The width is not a guess: the
parametric bootstrap supplies only the *shape* and its size comes from a
split-conformal calibration on the household's own past airings.

**One airing is genuinely hard to price, and hard to measure.** A median forecast
error of 52% is a real limitation and it is stated in the product, not buried
here. What it is *not* is unexplained: substituting the street's actual traffic
for its weekly forecast moves the median error by under 8%, and substituting the
day's actual outdoor air for its estimate moves it by under 1%. The uncertainty
is not mostly next week's weather. A single airing is a small signal on a
fifteen-minute cadence against a counterfactual that has to be inferred.

Which is exactly why the product's claim is a **comparison between two openings
on the same day**. The regional background, the day's weather, and the level the
flat happens to be sitting at are common to both sides of that comparison and
cancel in the difference. The decision card leads with the comparison and reports
the absolute figure with its interval underneath, in that order, deliberately.

---

## What the benchmark does not cover

- **The wire.** For speed the benchmark builds the observation table straight
  from the simulated home rather than through HTTP;
  `tests/test_tabulate_matches_store.py` pins the two paths against each other
  row by row on a short record, and `sash demo` runs the whole 120 days through
  the real webhook receiver and the real client.
- **Real households.** Every number here is against a simulator whose physics
  SASH's estimator was designed for. That is the right test of the estimator and
  it is not a test of whether a real flat obeys a single-zone mass balance. The
  answer to that is a live deployment, and `docs/RING_API_NOTES.md` says what
  the first hour of one should check.
- **Real Ring behaviour.** In particular, whether the Air Quality Monitor's
  reported value is instantaneous or a trailing average, which is undocumented
  and which the benchmark simply assumes correctly because the simulator and the
  estimator agree on it by construction. That is the single assumption most
  worth checking against a device, and it is friction log entry 3.
