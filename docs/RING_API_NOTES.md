# Ring API notes

What SASH calls, what it assumes, and where the documentation left a decision to
the integrator. Established against `developer.amazon.com/docs/ring/*` during
the hackathon window, and pinned by `tests/test_ring_contract.py`, which feeds
the reference pages' own example payloads to the shipping parsers.

Everything here is also implemented, so the code is the second copy of this
document: `src/sash/ring/`, and `simulator/ring_api.py` serves the same contract
locally.

---

## Endpoints

Base URL `https://api.amazonvision.com`.

| Path | Used for |
|---|---|
| `GET /v1/devices` | Discovery, with `include=status,capabilities,location,configurations` |
| `GET /v1/devices/{id}` | Device detail |
| `GET /v1/devices/{id}/status` | The Air Quality Monitor's last reported reading |
| `GET /v1/devices/{id}/capabilities` | Family hints and battery support |
| `GET /v1/devices/{id}/location` | Shown in setup |
| `GET /v1/devices/{id}/configurations` | Change detection on motion zones |
| `GET /v1/history/devices/{id}/events` | Camera event history, `event_types=motion.vehicle` |
| `GET /v1/users/me` | Account identity at link time |

Media endpoints (`/media/video/download`, `/media/image/download`,
`/media/streaming/whep/sessions`) are refused by the client before a socket
opens. See `docs/PRIVACY.md`.

**The device list is a JSON:API compound document.** Each device is
`{"type": "devices", "id": "ava1.ring.device.…", "attributes": {"name"},
"relationships": {...}}`, and the resources named in `include` arrive once each
in a top-level `included` array, matched by their `(type, id)` pair.
`RingClient.list_devices` joins them onto each device under
`attributes.status`, `attributes.capabilities` and so on, which is the shape the
rest of SASH reads.

**There is no family field on a device.** SASH decides what a device is from
what it reports: a status carrying `pm2_5` is the Air Quality Monitor, one
carrying `contact_detection` is a contact sensor, capabilities declaring `video`
are a camera. The result is advisory; the role a device plays in the home is set
on the Setup tab and stored beside it, which is where Ring's documentation says
that mapping belongs.

## OAuth

- Authorize: `https://account.ring.com/account/integrations/partner-link/authorize`
- Token: `https://oauth.ring.com/oauth/token`
- Scope `ava`. Standard authorization-code exchange and refresh; the access
  token lives about four hours and the refresh token about thirty days, so
  `TokenService` refreshes ten minutes early and marks the household
  `relink_required` the moment a refresh is rejected.
- The Playground issues a one-click token valid for thirty minutes.
  `RING_ACCESS_TOKEN` drops it into `StaticTokenService`, so `sash spike` and
  `sash live` run the same client code path with no OAuth round trip.

## Webhooks

- Header `X-Signature`, HMAC-SHA256, hex, `sha256=` prefixed, over the raw
  request body. SASH also accepts the bare digest.
- 200 expected within 5 seconds, retries otherwise. So the webhook route
  verifies, deduplicates, enqueues and returns; nothing else happens in the
  request path.
- `meta.request_id` is the idempotency key. SASH returns **200 for a duplicate**,
  because from Ring's side a retry that is acknowledged is a retry that stops.
  An invalid signature gets 401, and the count of them is a CloudWatch alarm.
- The envelope is payload version 1.1: `meta.{version, time, request_id,
  account_id}` and `data.{id, type, attributes, relationships}`. SASH routes on
  `data.type` and reads the device from `data.attributes.source`, never from
  `data.id`, whose structure is not a contract.
- `attributes.timestamp` is epoch **milliseconds** and is the event time.
  `meta.time` is when Ring sent the delivery.
- Motion events carry `attributes.sub_type` in `motion`, `human`, `vehicle`,
  `other_motion`. SASH keeps `vehicle` and discards the rest at the door.
- Sensor detection events carry the whole signal in `data.type`
  (`contact_sensor_faulted`, `contact_sensor_cleared`); no attribute carries
  the new state. Threshold events add the reading: `pm25_level` on
  `pm25_exceeded` and `pm25_cleared`, `temperature` and `humidity` on theirs.

## Rate limits

100 requests per second per partner. `X-RateLimit-Limit` and
`X-RateLimit-Remaining` on every response; 429 carries `Retry-After`. The client
mirrors both headers into `RateLimitState` for the Setup tab and backs off
exponentially, honouring `Retry-After` when present.

SASH's steady-state load is one status poll per Air Quality Monitor per five
minutes. It matters at link time, when discovery and camera history run
together.

---

## The facts that decide whether the model works

### 1. `contact_sensor_faulted` means the opening is OPEN

`faulted` is the magnet and the reed switch being apart. `cleared` is closed. The
documentation says so in a callout, and the vocabulary is still the alarm
panel's rather than the application's. Reading it the intuitive way inverts
every downstream estimate.

Isolated in `events.contact_state()`, pinned by one test, and referenced nowhere
else in the codebase.

### 2. The averaging window of an AQM reading is undocumented

Instantaneous, or a mean over the preceding cadence? The two imply covariate
alignments half a reporting period apart. On the simulator, the wrong choice
moved a recovered air exchange rate from 4.20 air changes per hour to 0.20.

`intervals.SAMPLE_LAG_MIN` is the assumption, in one place, with
`LAG_CANDIDATES` beside it for a per-household calibration against the home's
own contact transitions. Friction log entry 2.

### 3. Status returns the last reported reading, and `reported_at` is its time

The Air Quality Monitor reports temperature, humidity, PM2.5 and CO about every
fifteen minutes. Polling faster returns the same measurement again, and the
documentation notes that a reading can lag the response by up to an hour.
Storing repeats as measurements flattens every decay in the record and makes a
home look far leakier than it is.

`reported_at` arrives as an ISO 8601 string in `attributes`, and a device that
has not checked in yet reports `null` or the Unix epoch there.
`events.epoch_seconds` reads every documented form and returns nothing for a
placeholder, and `reported_at` is part of the primary key of `aq_samples`, so a
repeat is refused and counted as `aq_repeat_readings`, which is a health signal.
On the demo home's 120-day seed: 21,810 polls, 10,905 samples, 10,905 repeats
refused.

The status field is `pm2_5`; the webhook field is `pm25_level`. Temperature is
Celsius, humidity a percentage, PM2.5 µg/m³, all floats. Ring's own product
alert thresholds are PM2.5 above 35 µg/m³ and CO above 9 ppm, and SASH uses
those rather than inventing its own.

### 4. Webhooks are the only source of sensor events

Camera events have `/v1/history/devices/{id}/events`, and it returns only
events after the household granted consent. Sensors have no history endpoint,
which the documentation states, so a sensor integration's record starts at link
time.

This is a product constraint: SASH needs roughly 30 to 60 days of a household's
airings before it will report an opening at all, and the UI states each
opening's own reason rather than showing a spinner. Friction log entry 1.

---

## Smaller things worth knowing

- **Capability keys are shared across device families**, so a capability probe
  alone is not a family test. SASH reads the status attributes first and stores
  the family once at discovery.
- **Ring does not report what a contact sensor is attached to.** The
  documentation tells integrators to keep that mapping application-side, and
  `sash live` assigns each discovered device the role its family implies so the
  Setup tab opens configured.
- **`battery_status.percentage` reads 255 on the Air Quality Monitor**, which is
  mains powered; capabilities carry `battery_status: null` for it. SASH reads
  neither, and the simulator reports both the way the device does.
- **`device_online` is a heartbeat on sensors**, emitted on every uplink, and
  `device_offline` follows a heartbeat timeout. SASH reconstructs camera outages
  from the pair and excludes those intervals from every street term.
- **The Playground (May 2026) simulates Package, Vehicle and Motion camera
  events** and issues a token without an app. Sensor events come from
  `simulator/`, which serves the same contract.
- **Sensors and dynamic scopes went Early Access on 24 August 2026.** Sensor
  API calls can ship in an app now; publication waits on dynamic scopes reaching
  general availability.

---

## Running against Ring

```bash
export RING_ACCESS_TOKEN=<Playground token>
sash spike     # account, devices, every status, two polls: the second is refused as a repeat
sash live      # the product, discovering devices and polling every five minutes
```

`sash spike` prints what the account returned, exactly, so a difference from
this document shows up on the first run. `tests/test_api.py::test_live_discovers_and_polls_through_the_documented_endpoints`
runs the same sequence over HTTP against the simulator.
