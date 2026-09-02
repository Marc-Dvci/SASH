# Architecture

```
Ring                          SASH                                 Household
────                          ────                                 ─────────
contact sensors  ──webhook──▶ /ring/webhook ──▶ queue ──▶ store
camera (motion)  ──webhook──▶   verify HMAC              (SQLite)
device lifecycle ──webhook──▶   dedupe request_id            │
                                enqueue, 200                 │
                                                             ▼
Air Quality       ◀──poll───   AirQualityPoller ────────▶ store
Monitor           GET /v1/devices/{id}/status
                                                             │
                                                             ▼
                                                    ┌────────────────┐
                                                    │  Household     │
                                                    │  (one cached   │
                                                    │   view)        │
                                                    └────────┬───────┘
                                                             │
                    intervals ─▶ sources ─▶ estimator ─▶ nulls
                                                │            │
                                          activity ──▶ planner ──▶ /api/*  ──▶ UI
                                                             │
                                                          holdout
```

Two halves that deliberately do not touch.

**The Ring half** is `ring/` and the `/ring/*` routes. It verifies, deduplicates,
enqueues and acknowledges. It does no analysis, because Ring expects a 200 inside
five seconds and retries otherwise.

**The product half** is `analytics/`, `service.py` and the `/api/*` routes. Every
`/api` route is a read of one cached household view. Nothing under `/api` fits a
model or touches the network.

---

## The modules

| Module | Job |
|---|---|
| `ring/events.py` | The event vocabulary and envelope normalisation. `contact_state()` is the only place that knows `contact_sensor_faulted` means **open**. |
| `ring/webhook.py` | HMAC-SHA256 over the raw bytes, `request_id` idempotency, enqueue. |
| `ring/client.py` | Async REST client with a **path allowlist**. Media paths raise `MediaAccessDenied` before a socket opens. Absorbs `X-RateLimit-*`, backs off on 429. |
| `ring/auth.py` | OAuth code exchange and refresh, at-rest obfuscation, plus `StaticTokenService` for the Playground's one-click token. |
| `ingest.py` | `EventProcessor` applies one event. `Pipeline` is the queue (SQS-shaped). `AirQualityPoller` polls device status. `discover_devices` walks `/v1/devices`. |
| `store.py` | SQLite. `reported_at` is the primary key of `aq_samples`, so a repeated reading is refused rather than stored. |
| `analytics/intervals.py` | The observation table. Open fractions, activity, camera-outage exclusion, episode ids. |
| `analytics/sources.py` | Indoor-emission detection by interval balance on the CO and humidity channels. |
| `analytics/estimator.py` | Sealed-home baseline, CO tracer decay, the non-negative one-sided incoming fit, the paired block bootstrap. |
| `analytics/nulls.py` | The permutation test on the coupling gap. |
| `analytics/activity.py` | The street's weekly rhythm, with outages excluded. |
| `analytics/planner.py` | Demand, plan search, prediction interval, habit baseline, exploration, and the retrospective verification. |
| `analytics/holdout.py` | The three-slice replay: fit, calibrate the interval, score coverage. |
| `narrative.py` | Deterministic phrase renderer, with an optional Amazon Bedrock adapter behind the same interface. |
| `service.py` | `Household`: one cached view feeding every route, invalidated when new data arrives or the device mapping changes. |
| `api.py` | FastAPI. The two halves above. |
| `cli.py` | `sash demo | seed | serve | fit | bench`. |
| `web/` | Five views, no framework, no build step, inline SVG charts. |

---

## The simulator

`simulator/` is a Ring account, not a fixture loader. It exists because the Ring
Playground simulates camera events but not sensor events, so a sensor-first
integration cannot be developed against it (friction log entry 1).

- `home.py` is a single-zone mass balance producing PM2.5, humidity, temperature
  and CO on a one-minute grid, driven by a regional background, a street activity
  process, an occupancy habit and indoor sources. It is the **ground truth**: the
  benchmark scores SASH against it and SASH never sees it.
- `ring_api.py` serves the documented Ring endpoints for that home, including the
  three media endpoints, which **do answer**. That is deliberate: it means the
  client-side refusal is what `tests/test_no_media.py` proves, against a server
  that would have complied.
- `seed.py` replays a household's history through the **real** webhook receiver
  and the **real** client. Signed payloads, `request_id` retries, repeated status
  reads. A bug in signature verification, idempotency, the `reported_at` dedupe
  or the inverted contact vocabulary shows up on the first run.
- `tabulate.py` builds the observation table straight from a `SimResult`, for the
  benchmark, where forty households at four months each through HTTP would cost
  hours. `tests/test_tabulate_matches_store.py` pins it against the store path
  row by row.

`sash demo` runs the product with that simulated account still attached, so
**Advance** delivers events the same way: signed webhooks to `/ring/webhook`, and
readings fetched by the real client from `GET /v1/devices/{id}/status`.

---

## Data flow, one interval at a time

1. A contact sensor faults. Ring posts a signed webhook. `WebhookReceiver`
   verifies over the raw bytes, records the `request_id`, enqueues, returns 200.
2. `EventProcessor` maps `contact_sensor_faulted` to **open** and writes a
   transition.
3. Fifteen minutes later the Air Quality Monitor reports. The poller reads
   `GET /v1/devices/{id}/status`, sees a new `reported_at`, and stores a sample.
   The next poll sees the same `reported_at` and is refused.
4. `intervals.build` pairs that reading with the previous one, computes how much
   of the gap each opening was open for, counts vehicle-motion events over the
   same window, and marks the interval if the camera was offline for any of it.
5. `sources.detect` decides whether an indoor emission was running.
6. `estimator.fit` measures the sealed baseline, then each opening's exchange
   rate on the CO channel, then every opening's coupling in one joint fit.
7. `nulls.rank_openings` decides whether the difference survives a shuffle.
8. `planner.recommend` searches openings and start times and returns a plan with
   an interval.
9. The household publishes it. `planner.verify` scores it once its window has
   passed.

---

## Storage

SQLite, one file. Tables: `devices`, `webhook_receipts`, `vehicle_events`,
`contact_events`, `aq_samples`, `aq_threshold_events`, `device_health`, `plans`,
`meta`.

The two keys that carry a rule rather than a row:

- `webhook_receipts.request_id` is unique. A retry is absorbed and counted.
- `aq_samples(device_id, reported_at)` is unique. The status endpoint returns the
  last reported reading, so polling faster than the device reports yields repeats,
  and a repeat is a health signal, never a measurement.

---

## The AWS deployment

`infra/template.yaml` is a SAM stack with the same shape: API Gateway to a
webhook Lambda, SQS to a processor Lambda, DynamoDB, EventBridge Scheduler for
polling, Secrets Manager with a KMS key for the Ring credentials, CloudWatch
alarms on the queue and on rejected signatures. `Pipeline` is already
SQS-shaped, so the local queue and the deployed one differ in one adapter.

`narrative.py` carries an Amazon Bedrock adapter behind an interface whose
default is a deterministic phrase renderer, so the wording of the decision card
can be produced by a model or by code and the **numbers are identical either
way**. The model is never asked what to recommend, only how to say it.

The stack has not been deployed. See `docs/AWS.md`.
