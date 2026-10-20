# Benchmark

24 simulated households plus 40 negative
controls, 120 days of Ring streams each, scored against the
simulator's own physics. Reproduce with `sash bench --homes 24 --nulls 40 --days 120 --seed 1`.

## Recovery

| quantity | median error |
|---|---|
| Sealed-home loss rate | 0.033 per hour absolute |
| Air exchange rate, per opening | 7.1% relative |
| Street coupling, per opening | 1.06 ug/m3 per event/min |

62 of 62 openings passed the reporting
gate and carry a number. The rest are shown as still learning, with the reason.

## Ranking, and what the gate is for

SASH named the truly least-coupled opening in 19 of 24 households
(79.2%). That number mixes two questions. In
16 of those households the permutation gate refused to call a
winner, and it refused on the right ones: the median true gap between the two
openings is 6.71 ug/m3 per event/min where SASH
made the claim and 0.83 where it withheld it.

The verdict SASH actually shows is per pair of openings, and a three-opening
home produces two of them. Across 38 pairs, the gate called
15 and withheld the rest. **Of the pairs it called, the
opening it named as cheaper really was cheaper in
14 of 15
(93.3%).** The median true gap is
8.40 on the pairs it called and
1.45 on the pairs it withheld.

## Decision quality

68 of 87 recommendations (78.2%) really did cost
less than the household's own habitual airing, measured by re-integrating the flat
in the simulator on the record that actually happened. Median realised saving
37.5%, against a median forecast saving of
50.9%.

Restricted to the households where the openings are distinguishable at all:
**22 of 27**
(81.5%), median realised saving
**46.4%**.

## Stepping aside

At 192 moments ten minutes after a household stopped cooking, the
planner without its step-aside rules told the household to wait more than two
hours in **0** (longest 0.2 h).
With the rules: **0** (longest 0.0 h).
SASH recognised the cooking and switched to "air now" at
187 of the 192.

At the 126 ordinary decision points a rule fired at
42. Across both kinds of moment the rules changed
22 plans; 12 of those cost more street
particulate than the plan they replaced, median difference
53.3 ug/m3 x min (the replaced plans' median
cost: 55.5).

## False claims on openings that are identical

**2 of 40** null households
(5.0%, Wilson 95% 1.4% to 16.5%)
had a difference announced where none exists. The permutation gate runs at
alpha = 0.05 over 400 shuffles.

## Pricing one airing

3517 held-out airings, each forecast before it happened, measured
afterwards from the readings, and compared with the simulator's own answer.

| quantity | value |
|---|---|
| Median true cost of an airing | 681.6 ug/m3 x min |
| Median forecast error | 354.3 (52% of it) |
| Median error of the retrospective measurement | 177.9 |
| Median bias of that measurement | -39.3 |
| Coverage of the 90% interval | 88.6% |

One airing is genuinely hard to price, and it is hard to measure afterwards as
well. That is why the product's claim is a comparison between two openings on
the same day rather than an absolute number: the terms that make a single
airing uncertain are common to both sides of it.

Ran in 501 s.
