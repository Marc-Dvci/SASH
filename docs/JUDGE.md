# For a judge, in ten minutes

**SASH tells a household which window to open, and when, so the flat gets the air
it normally gets while letting in as little street pollution as possible.** It
runs on three Ring devices and never looks at an image.

---

## Two minutes: see it

```bash
pip install -e ".[dev]"
sash demo
```

Open <http://127.0.0.1:8000>. First run seeds 120 days through the Ring ingest
path in about ninety seconds with a progress bar; later runs start in a second.
No Ring credentials needed for the demo.

Then, in this order:

1. **Now**: the decision. One opening, one time, one duration, with the
   comparison against what this household actually does.
2. **Windows**: the two measured numbers per opening, with intervals, and the
   verdict. One pair reads **"these two openings are not yet
   distinguishable"**. That is a shipped output.
3. **Advance 30 min** (top right): Ring events arrive live, signed webhooks
   through the receiver, readings fetched by the client.
4. **Now, Publish this plan, The household follows it, Advance twice.**
5. **Record**: the plan is now scored against the readings that followed.

Or watch it drive itself: <http://127.0.0.1:8000/?demo=1>. The same product is
deployed on AWS at
<https://i1jcuqoto7.execute-api.us-east-1.amazonaws.com/dev/>.

With a Playground token, `RING_ACCESS_TOKEN=<token> sash spike` reads a Ring
account through the same client: account, devices, every status, two polls.

---

## Four minutes: the three things that are not obvious

**1. The measurement.** Each opening has two unknowns: how fast it moves air
(the benefit) and how dirty the air it moves is (the cost). On fifteen-minute
particle readings those trade off badly. **Carbon monoxide has no street term in
it**, so between cooking events the CO channel is a decaying tracer, and
regressing its decay rate on how open each window was gives that window's air
exchange rate directly. That is tracer-gas decay, the building science standard
for measuring ventilation, which normally needs a technician and a cylinder of
tracer gas. The household makes the tracer twice a day by cooking and Ring
already measures it. With the exchange rate known, the particle channel has one
unknown left. `docs/MODEL.md`.

**2. The refusal is the product.** Before SASH names a better window, every
airing is held exactly as it happened and only the label is shuffled. On 40
simulated households whose two openings are **physically identical**, SASH
announced a difference on 2, at a nominal 5%. Across the 38 real comparisons it
called 15 and withheld 23, and the median true gap is 8.40 where it spoke and
1.45 where it did not. **Of the 15 it called, 14 were right.**
`docs/BENCHMARK.md`.

**3. It is built on a camera and never looks through it.** Ring's camera scope
carries the media endpoints together with the motion events SASH needs. So the
client carries a ten-entry path allowlist and `tests/test_no_media.py` walks
every documented media path against a simulator that **implements them and
would answer**, asserting the refusal happens before a socket opens. Human
motion is delivered by Ring and discarded at the door: 10,300 dropped against
72,099 kept. `docs/PRIVACY.md`.

---

## Four minutes: check the work

```bash
pytest                                            # 155 tests
sash bench --homes 24 --nulls 40 --days 120       # about 8 minutes
```

The benchmark scores against `simulator/home.py`, a single-zone mass balance SASH
never sees. `tests/test_ring_contract.py` feeds Ring's own reference payloads,
copied verbatim from the Device Discovery, Sensors and Motion Detection pages,
to the shipping parsers.

| | |
|---|---|
| Air exchange rate recovered to | 7.1% median relative error |
| Pairs called correctly | 14 of 15 |
| False claims on 40 identical-opening homes | 2 (5.0%) |
| Recommendations that beat the household's habit | 71 of 94, median saving 44.5% |
| Coverage of the 90% interval, 3,517 held-out airings | 88.6% |

A single airing has a median forecast error of 52% of its true cost, and the
benchmark locates it: substituting the actual traffic and the actual outdoor air
moves it by under 10%, so a single airing is a small signal on a fifteen-minute
cadence. That is why the claim SASH makes is a comparison between two openings
on the same day, where those terms cancel.

---

## Where to look in the code

| Question | File |
|---|---|
| Is Ring technology actually called at runtime? | `src/sash/ring/client.py`, `src/sash/ring/webhook.py`, `src/sash/live.py`, `src/sash/api.py` |
| Does the client read Ring's documented shapes? | `tests/test_ring_contract.py` |
| How is the exchange rate measured? | `estimator.tracer_decays`, `estimator.fit_tracer` |
| How does it refuse to over-claim? | `analytics/nulls.py` |
| How is a plan scored afterwards? | `planner.realised_intake`, `planner.verify` |
| Is the media refusal real? | `ring/client.py:ALLOWED_PATHS`, `tests/test_no_media.py` |
| Where is the evidence? | `bench/run_benchmark.py`, `bench/results/` |
| What runs on AWS? | `infra/template.yaml`, `src/sash/aws/`, `docs/AWS.md` |
