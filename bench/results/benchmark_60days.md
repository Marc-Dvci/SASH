# Benchmark

12 simulated households plus 0 negative
controls, 60 days of Ring streams each, scored against the
simulator's own physics. Reproduce with `sash bench --homes 12 --nulls 0 --days 60 --seed 1`.

## Recovery

| quantity | median error |
|---|---|
| Sealed-home loss rate | 0.033 per hour absolute |
| Air exchange rate, per opening | 11.9% relative |
| Street coupling, per opening | 0.89 ug/m3 per event/min |

29 of 30 openings passed the reporting
gate and carry a number. The rest are shown as still learning, with the reason.

## Ranking, and what the gate is for

SASH named the truly least-coupled opening in 7 of 12 households
(58.3%). That number mixes two questions. In
10 of those households the permutation gate refused to call a
winner, and it refused on the right ones: the median true gap between the two
openings is 6.18 ug/m3 per event/min where SASH
made the claim and 1.49 where it withheld it.

The verdict SASH actually shows is per pair of openings, and a three-opening
home produces two of them. Across 17 pairs, the gate called
2 and withheld the rest. **Of the pairs it called, the
opening it named as cheaper really was cheaper in
2 of 2
(100.0%).** The median true gap is
6.18 on the pairs it called and
2.24 on the pairs it withheld.

## Decision quality

31 of 41 recommendations (75.6%) really did cost
less than the household's own habitual airing, measured by re-integrating the flat
in the simulator on the record that actually happened. Median realised saving
32.9%, against a median forecast saving of
58.9%.

Restricted to the households where the openings are distinguishable at all:
**6 of 6**
(100.0%), median realised saving
**40.9%**.

## Stepping aside

At 96 moments ten minutes after a household stopped cooking, the
planner without its step-aside rules told the household to wait more than two
hours in **2** (longest 11.0 h).
With the rules: **0** (longest 0.0 h).
SASH recognised the cooking and switched to "air now" at
95 of the 96.

At the 65 ordinary decision points a rule fired at
15. Across both kinds of moment the rules changed
7 plans; 2 of those cost more street
particulate than the plan they replaced, median difference
-61.2 ug/m3 x min (the replaced plans' median
cost: 20.5).

## False claims on openings that are identical

**0 of 0** null households
(--, Wilson 95% 0.0% to 100.0%)
had a difference announced where none exists. The permutation gate runs at
alpha = 0.05 over 400 shuffles.

## Pricing one airing

216 held-out airings, each forecast before it happened, measured
afterwards from the readings, and compared with the simulator's own answer.

| quantity | value |
|---|---|
| Median true cost of an airing | 638.8 ug/m3 x min |
| Median forecast error | 564.0 (88% of it) |
| Median error of the retrospective measurement | 210.8 |
| Median bias of that measurement | 4.0 |
| Coverage of the 90% interval | 88.9% |

One airing is genuinely hard to price, and it is hard to measure afterwards as
well. That is why the product's claim is a comparison between two openings on
the same day rather than an absolute number: the terms that make a single
airing uncertain are common to both sides of it.

Ran in 64 s.
