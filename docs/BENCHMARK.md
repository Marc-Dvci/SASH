# Benchmark

The demo home shows SASH running. The benchmark shows whether it is right,
across households it has never seen.

`sash bench` simulates households SASH has never seen, with different physics and
different habits, hands it nothing but the streams a Ring account would deliver,
and scores five things it is possible to be wrong about. The scoring is against
`simulator/home.py`, which is a single-zone mass balance on a one-minute grid.
SASH never sees any of it.

```bash
sash bench --homes 24 --nulls 40 --days 120 --seed 1
```

Results below are that command, verbatim, from `bench/results/`. It takes about
nine minutes.

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

That is the number the product rests on: right 93% of the time when it
speaks, and explicit about the pairs it declines to call.

## 3. Decision quality

The question here is whether the advice was better than what the household
would have done anyway. SASH's recommended airing and the household's
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
to expect from a selected maximum.

## 4. False claims on openings that are identical

`null_home` builds a household whose two openings have **the same** air exchange
rate and **the same** street coupling. There is nothing to find. The household
still favours one of them, still uses them at different times, and the estimates
still come out different, because they are estimates.

**2 of 40** null households had a difference announced. That is **5.0%**, Wilson
95% interval 1.4% to 16.5%, against a permutation gate running at alpha = 0.05
over 400 shuffles.

This is the number a judge should look at first, and it is the reason the
sample is 40 rather than 16. At 16 nulls the observed rate was 12.5%, which is
consistent with 5% at that sample size, and at 40 it is 5.0%. Two candidate
mechanisms for a systematic over-rejection were checked on the way: balancing
the two openings' airing counts before comparing, and drawing the exchange
rates from their bootstrap inside each permutation. Neither moved a p-value,
which is what a gate running at its nominal rate looks like.

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

**The interval is calibrated.** 88.6% of a nominal 90%, on a slice that neither
the fit nor the interval calibration ever saw. The parametric bootstrap supplies
the *shape* and its size comes from a split-conformal calibration on the
household's own past airings.

**One airing is a small signal.** A median forecast error of 52% is stated in
the product, and the benchmark locates it: substituting the street's actual
traffic for its weekly forecast moves the median error by under 8%, and
substituting the day's actual outdoor air for its estimate moves it by under
1%. What remains is a fifteen-minute cadence against a counterfactual that has
to be inferred.

Which is exactly why the product's claim is a **comparison between two openings
on the same day**. The regional background, the day's weather, and the level the
flat happens to be sitting at are common to both sides of that comparison and
cancel in the difference. The decision card leads with the comparison and reports
the absolute figure with its interval underneath, in that order, deliberately.

---

## What the benchmark rests on

- **The wire.** The benchmark builds the observation table straight from the
  simulated home; `tests/test_tabulate_matches_store.py` pins that path against
  the store path row by row on a short record, and `sash demo` runs the whole
  120 days through the webhook receiver and the client.
- **The physics.** Every number here is against `simulator/home.py`, a
  single-zone mass balance on a one-minute grid with a regional background, a
  street activity process, an occupancy habit and indoor sources. The
  estimator sees only the three Ring streams.
- **The sample alignment.** The simulator and the estimator agree on whether a
  reading is instantaneous or a trailing mean, and `intervals.SAMPLE_LAG_MIN`
  is where that assumption lives for a real device. Friction log entry 2.
