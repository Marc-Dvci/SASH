# Onboarding

24 simulated households, each run twice on the same world for up to 45
days: once on its own habit, once following SASH's calibration steps with
probability 60% per step. SASH is refitted every morning on the
readings so far. Reproduce with `python bench/onboarding.py --homes 24 --days 45 --comply 0.6`.

| milestone, days since linking | habit, median | guided, median | homes by day 14 | homes by day 21 | homes by day 45 |
|---|---|---|---|---|---|
| First plan (one window measured) | 18 | **9** | 9 to **23** | 16 to **24** | 24 to 24 |
| Every window measured | 40 | **11** | 1 to **20** | 3 to **24** | 15 to 24 |
| First pair of windows called | 46 | **46** | 0 to **5** | 1 to **9** | 7 to 11 |

A home that had not reached a milestone by day 45 counts as day 46 in
the median. Paired on the same home, guidance measured every window a median
of **26 days** sooner (at least: a home
the habit never finished counts as day 46); it was faster in
24 of 24 homes and slower in
0. While calibrating, the guided household followed a
median of 0.9 prompted airings a day, thirty minutes
each, through the window it was asked to use.

Ran in 816 s.
