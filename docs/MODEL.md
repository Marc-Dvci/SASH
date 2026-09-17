# The model

SASH answers one question: **of the openings in this home, which one should be
opened, and when, to get the air the household normally gets while letting in the
least street-linked particulate?**

Answering it needs two numbers per opening that nobody has ever measured for any
particular flat:

- **theta**, the air exchange rate through that opening, in air changes per hour.
  This is the benefit. A window that moves more air satisfies the household's
  ventilation need in less time.
- **g**, the street coupling, in micrograms per cubic metre per vehicle-motion
  event per minute. This is the cost. It is what the air arriving through that
  particular opening carries when the street outside is busy.

Neither is a property of the street. Both are properties of *this opening in this
flat*, and the whole point is that they differ between two windows in the same
room. A courtyard window and a street window on the same wall of the same flat
can differ by a factor of five in g and still be within thirty per cent on theta.

Everything below is how those two numbers are recovered from three Ring streams
and nothing else: vehicle-motion event times from a camera, contact transitions
from sensors on the openings, and a four-channel Air Quality Monitor reporting
every fifteen minutes.

---

## 1. The unit of analysis is an interval, not a reading

The Air Quality Monitor reports about every fifteen minutes, and
`GET /v1/devices/{id}/status` returns the last reported reading rather than a
live one. The finest usable unit is therefore the gap between two consecutive
fresh readings, with everything else aggregated onto it: how much of that gap
each opening was open for, how many vehicle-motion events the camera saw, and
whether the camera was online at all.

Two properties of that table decide whether what follows is a model or an
artefact.

**An interval is not a derivative.** Over fifteen minutes a first-order system
does not move at its instantaneous rate; it moves along its exact solution. With
`K` the total removal rate over the interval, the reading that arrives is

```
C1 = C0 * exp(-K dt) + (1 - exp(-K dt)) / K * (b Cstar + o theta D)
```

where `b` is the sealed-home loss rate, `Cstar` the level the home settles at,
`o` the fraction of the interval the opening was open, and `D` what the air
coming through it carries. SASH predicts `C1` and compares candidates on the
reading each one implies. Residuals are then in micrograms per cubic metre of
measured particulate, on one scale, comparable across candidates.

Predicting the reading, rather than deconvolving the slope, is what makes the
residuals comparable: dividing the observed slope by the attenuation factor
multiplies the residuals by its reciprocal, so a candidate theta twice as large
arrives with residuals twice as large and a search on those residuals walks to
the bottom of the grid every time.

**An offline camera is not a quiet street.** A camera that is offline reports no
vehicle motion. Treating that as a quiet street is the easiest way in this whole
problem to invent a coupling, so intervals overlapping an outage are marked and
excluded from every street term. Outages are reconstructed from
`device_offline` and `device_online` lifecycle webhooks.

**Sample alignment is a stated assumption.** Whether a reported value is
instantaneous or an average over the preceding cadence is undocumented, and the
two imply alignments half a reporting period apart. On the simulator, the wrong
choice moves a recovered exchange rate from 4.20 to 0.20.
`intervals.SAMPLE_LAG_MIN` is that assumption, in one place, with the candidates
for a per-household calibration next to it. It is entry 2 in the friction log.

---

## 2. The tracer has no street in it

The particle channel carries both unknowns at once. How fast the opening moves
air and how dirty that air is trade off badly against each other on
fifteen-minute readings, because a small window onto a filthy street and a large
window onto a clean one produce similar rises.

Carbon monoxide separates them, and Ring ships it in the same device.

Outdoor CO in a residential street is negligible next to indoor combustion. So
between cooking events the CO channel is a **decaying tracer with no source
term**, and its decay constant is exactly what is wanted:

```
K_co = k_co + o_w theta_w
```

Regress the observed decay constant on the open fraction across the whole
record. The slope is that opening's air exchange rate. The intercept is the
home's own sealed CO loss, which is a property of the flat and comes out of the
same regression for free.

This is **tracer-gas decay**, the standard building-science method for measuring
ventilation rate, and it normally needs a technician, a cylinder of tracer gas
and an afternoon. The household is already generating the tracer twice a day by
cooking, and Ring is already measuring it.

The regression follows the upper envelope of the scatter rather than its centre
(`TAU_TRACER = 0.75`). An indoor source running during a decay makes that decay
look **slower**; nothing makes it look faster. The fast side of the scatter is
the side telling the truth.

With theta measured on the CO channel and held fixed, the particle channel has
one unknown left, and that is g.

**What the tracer needs from the device.** The decays SASH fits run from a
cooking peak of one to a few ppm down to a floor near 0.1 ppm
(`CO_TRACER_FLOOR_PPM = 0.12`), so the channel has to resolve hundredths of a
ppm; the simulator reports CO to 0.01 ppm with 0.02 ppm of noise, and Ring's
product alert at 9 ppm sits far above every decay the fit uses. The tracer is
what the reporting gate waits for: an opening is reported once enough of its
airings began while the CO channel was above that floor, and until then the
Windows tab says how many more it needs.

---

## 3. Detecting a source, not its onset

An indoor emission during an interval puts particulate in the room that did not
come through the window. Those intervals have to be found, because the estimator
would otherwise attribute a hob to whichever opening happened to be ajar.

The first version looked for a *rising* CO or humidity channel. It caught 10 of
17 contaminated intervals on the demo home, and the 7 it missed were the worst
ones: a household opens a window **while** it cooks, and by the second reading
the CO has stopped rising because the open window is carrying it away as fast as
the hob makes it. Readings sitting at 130 micrograms per cubic metre with the
window wide open were being handed to the fit as evidence about the window.

What SASH computes instead is the **emission rate implied by each channel's own
interval balance**:

```
s = (v1 - v0 exp(-K dt)) / M - K v_out,      M = (1 - exp(-K dt)) / K
```

which stays positive for as long as the source runs, whatever the reading is
doing. Above 0.4 ppm per hour on the CO channel the interval is flagged.

The other half of that job matters as much: **not** flagging good airings. The
best airings in a household's record happen right after cooking, when carbon
monoxide is high and falling fast because the window is open. There `s` is zero,
correctly, and the airing is kept.

What is left over is candles, a toaster, a vacuum cleaner: particles with no
combustion and no moisture. They are handled downstream by a loss that
**redescends above the fit and stays quadratic below it**, because an undetected
emission can only push a reading up. That asymmetry is the shape of the
contamination.

---

## 4. One background, one coupling per opening

With theta known and contaminated intervals removed, each opening's isolated
airings are fitted jointly for

```
D_w = Cstar_out + g_w * activity
```

with **one shared `Cstar_out`** across the whole home and one `g_w` per opening.
Sharing the background is the constraint that makes the comparison mean
something: the outdoor air on a quiet street is a property of the neighbourhood,
not of which window is open, so anything an opening admits above it has to be
explained by that opening's own coupling.

Both `Cstar_out` and every `g_w` are constrained non-negative. The fit is a
non-negative ridge, and the residual loss is the one-sided one from section 3.

The shared background was chosen by measurement: a separate background per
opening absorbs one opening's excess into its own background and loses the
ranking, and a three-column design with a floor-movement term is marginally
worse across every seed.

---

## 5. SASH will not name a better window until the label earns it

Two openings in one flat produce two different numbers even when they are the
same window twice, because the estimates are noisy and the household used them on
different days. A product that announces "your courtyard window is better" on
that basis is describing noise.

So before SASH says anything, every airing is held exactly as it happened and
only the **label**, which opening it belonged to, is shuffled. Under the null
that the two openings behave the same, that shuffle changes nothing real, so the
spread of the shuffled gaps is the spread the estimator produces from nothing.
The observed gap has to sit outside it.

The shuffle keeps whole airings together, because an airing is one draw of the
weather and splitting it into three readings would fake precision by a factor of
the square root of three. It keeps the two group sizes fixed, because a
difference between a 40-airing opening and a 12-airing one is partly a difference
in precision.

**"These two openings are not yet distinguishable" is a shipped output**, with a
screen of its own. On the benchmark it is also the reason the product is right
when it speaks: the households where SASH withholds the claim are the households
whose openings really are nearly identical.

---

## 6. From two numbers to a plan

**How much air.** The target is the household's **own historical median** air
changes per twelve hours, computed from its own record. SASH moves *when* a
household airs and *which* opening it uses, and never *how much* air it gets.
Recommending a shorter airing would score well on a particulate metric and
would be advice to under-ventilate a flat. The duration for each candidate is
therefore `60 * target / theta_w`, so every candidate delivers the same air, and
`tests/test_planner.py` pins that invariant.

**When.** Every opening at every quarter hour over the next twelve hours is
integrated forward through the same mass balance, against this street's own
weekly rhythm. One filter is applied to the clock: SASH will not offer a time
this household has never aired at. The quietest street of the week is at four in
the morning and no plan that starts there will ever be acted on.

**Which.** The candidate with the lowest predicted intake wins, with ties inside
eight per cent broken towards the earlier plan, because a household will not hold
its breath until the evening to save two per cent.

**How sure.** The comparison against the household's habitual airing is made on
**paired draws**: the plan and the habit are forecast together on each draw of
the bootstrap, so they share the same weather, the same day and the same
background, and what is left is the difference between the two openings. That is
what "cheaper in 97% of paired draws" means on the decision card.

---

## 7. The interval around one airing, and why it is wide

The prediction interval does not come from the parameter bootstrap. The bootstrap
knows how uncertain theta and g are; it does not know what next Tuesday's air
will be like. So the parametric half-width is used only as a **shape** and its
size is set by **split-conformal calibration** on the household's own past
airings: fit on the earliest slice, price the airings in the middle slice, take
the quantile of how far they landed from their forecasts in units of that shape.
Coverage is then measured on a third slice that neither the fit nor the
calibration has seen.

On the benchmark that comes out at 88.6% coverage of a nominal 90% interval over
3,517 held-out airings, which is the number that says the width is calibrated.

The width itself is large, and the benchmark says why. Against the simulator's
own ground truth, the median error of a **forecast** of one airing is 354
micrograms per cubic metre times minutes on a median airing of 682, and the
median error of the **retrospective measurement** of the same airing is 178. One
airing is hard to price in advance and hard to measure afterwards.

The benchmark locates that width. Substituting the street's actual traffic for
its forecast, or the day's actual outdoor air for its estimate, moves the median
error by under ten per cent. What remains is that a single airing is a small
signal measured on a fifteen-minute cadence against a counterfactual that has to
be inferred.

Which is exactly why the product's claim is a **comparison** rather than an
absolute number. The terms that make one airing uncertain, the regional
background, the day's weather, the level the flat happens to be sitting at, are
common to both sides of it and cancel in the difference. The decision card leads
with the comparison and reports the absolute figure with its interval underneath.

---

## 8. Verification: a plan that was written down can be wrong

Every plan SASH publishes is written into a ledger with its forecast and its
interval **before** the airing happens. When the window it named has passed, the
airing is measured from the readings that arrived: a counterfactual shadow
trajectory is integrated forward from the reading before the opening opened,
using only the sealed-home terms, and the realised intake is the area between
that shadow and the readings that actually arrived.

Three rules govern the scoring.

**A plan is scored only when the household acted on it.** If the named
opening was open for less than 40% of the window, the plan is recorded as not
followed and left unscored. SASH does not open windows and takes credit and blame
only for airings it proposed that actually happened.

**Contamination during the airing makes it unverifiable.** If an indoor
source ran, or a second opening was open, there is no isolated airing to
measure, and the plan is recorded as unscored.

**Contamination after the airing truncates the tail.** The forecast prices the
airing plus a ninety-minute recovery tail, because load admitted through a window
is still in the room after the window shuts. A household that airs at eleven and
cooks at half past one has a perfectly measurable airing and a contaminated tail.
The tail is integrated only while it stays clean and the rest of it is
completed at the home's own measured decay rate, which is what the forecast
assumed the room would do anyway; on the demo home that keeps 43 scorable
airings rather than 29.

The floor the shadow decays to is measured **retrospectively**, from the quiet
readings on either side of the airing, rather than carried over from whenever the
model was last fitted. The sealed floor moves with the weather, and a
counterfactual anchored to last month's floor measures the weather rather than
the window. It is the fifteenth percentile of those quiet readings; against
ground truth on the benchmark that measurement has a median bias of −39 on a
median airing of 682.

---

## 9. What the claim is

- **A comparison between two openings on the same day.** The regional
  background, the weather and the level the flat sits at are common to both
  sides and cancel in the difference. The decision card leads with it.
- **A ranking that survived a shuffle.** An opening is named cheaper only when
  the difference outlives the permutation of labels in section 5.
- **This street's own weekly rhythm as the forecast.** The planner integrates
  against the median activity for this hour of this weekday, and the UI says
  so where the profile is drawn.
- **An outdoor estimate that is a by-product of the fit.** `Cstar_out` is
  shared across the openings and shown on the Windows tab as the quiet-street
  level the home's own record implies.
- **Event metadata only.** Vehicle classification arrives as a field on a
  webhook. The client carries a path allowlist and refuses every media endpoint
  before a socket is opened.
