# Ring API notes

What SASH calls, what it assumes, and where the documentation left a decision to
the integrator. Established against `developer.amazon.com/docs/ring/*` and the
Ring developer material during the hackathon window.

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
| `GET /v1/devices/{id}/capabilities` | Advisory only, see "capability keys" below |
| `GET /v1/devices/{id}/location` | Shown in setup |
| `GET /v1/devices/{id}/configurations` | Change detection on device settings |
| `GET /v1/history/devices/{id}/events` | Camera history backfill. **No sensor equivalent exists** |
| `GET /v1/users/me` | Account identity at link time |

Media endpoints (`/media/video/download`, `/media/image/download`,
`/media/streaming/whep/sessions`) are refused by the client before a socket
opens. See `docs/PRIVACY.md`.

## OAuth

- Authorize: `https://account.ring.com/account/integrations/partner-link/authorize`
- Token: `https://oauth.ring.com/oauth/token`
- Standard authorization-code exchange and refresh. No surprises.
- The Playground issues a one-click token valid for thirty minutes.
  `ring/auth.py:StaticTokenService` takes that token directly, so the same client
  code path runs against the Playground with no OAuth round trip.

## Webhooks

- Header `X-Signature`, HMAC-SHA256, hex, `sha256=` prefixed. SASH also accepts
  the bare digest, which costs nothing and has bitten other integrations during
  header rewrites at a proxy.
- **Verify over the raw request body.** Re-serialising parsed JSON changes key
  order and whitespace and the digest stops matching. Friction log entry 9.
- 200 expected within 5 seconds, retries otherwise. So the webhook route verifies,
  deduplicates, enqueues and returns; nothing else happens in the request path.
- `meta.request_id` is the idempotency key. SASH returns **200 for a duplicate**,
  because from Ring's side a retry that is acknowledged is a retry that stops.
  An invalid signature gets 401, because a retry of an unsigned body will not help
  anyone and the count of them is worth watching.
- The envelope is `{"meta": {...}, "data": {"type", "id", "attributes"}}`. Ring's
  examples have shown `attributes` both nested under `data` and at the top level,
  so `events.attributes()` accepts either shape rather than guessing one.
- `attributes.timestamp` is epoch **milliseconds**.

## Rate limits

100 requests per second per partner. `X-RateLimit-Limit` and
`X-RateLimit-Remaining` on every response; 429 carries `Retry-After`. The client
mirrors both headers into `RateLimitState` for the health page and backs off
exponentially, honouring `Retry-After` when present.

SASH's steady-state load is one status poll per Air Quality Monitor per five
minutes, so the limit is not close to binding. It matters at link time, when
discovery and camera history backfill run together.

---

## The four facts that decide whether the model works

### 1. `contact_sensor_faulted` means the opening is OPEN

`faulted` is the magnet and the reed switch being apart. `cleared` is closed. The
vocabulary is right from the alarm panel's point of view and inverted from the
application's, and reading it the intuitive way inverts every downstream estimate
without raising anything.

Isolated in `events.contact_state()`, pinned by one test, and referenced nowhere
else in the codebase.

### 2. The averaging window of an AQM reading is undocumented

Instantaneous, or a mean over the preceding cadence? The two imply covariate
alignments half a reporting period apart. On the simulator, the wrong choice
moved a recovered air exchange rate from 4.20 air changes per hour to 0.20.

SASH does not guess silently. `intervals.SAMPLE_LAG_MIN` is the assumption, in
one place, with `LAG_CANDIDATES` beside it for a per-household calibration
against the home's own contact transitions. Friction log entry 3, and the most
consequential undocumented fact in the API for this class of application.

### 3. Status returns the last reported reading, not a live one

The Air Quality Monitor reports temperature, humidity, PM2.5 and CO roughly every
fifteen minutes. Polling faster returns the same measurement again. Storing those
repeats as measurements flattens every decay in the record and makes a home look
far leakier than it is.

`reported_at` is part of the primary key of `aq_samples`. A repeat is refused and
counted as `aq_repeat_readings`, which is a health signal. On the demo home's
120-day seed: 21,810 polls, 10,905 samples, 10,905 repeats refused.

Ring's own product alert thresholds are PM2.5 above 35 ug/m3 and CO above 9 ppm,
and SASH uses those rather than inventing its own.

### 4. There is no event-history endpoint for sensors

Camera events have `/v1/history/devices/{id}/events`. Sensors do not. Webhooks
are the only source, so a sensor integration's record starts at link time.

This is a product constraint, not an implementation detail: SASH needs roughly 30
to 60 days of a household's airings before it will report an opening at all, and
the whole of that wait exists because of this gap. The UI states each opening's
own reason rather than showing a spinner. Friction log entry 2.

---

## Smaller things worth knowing

- **Capability keys are shared across device families**, so a capability probe is
  not a reliable family test. SASH switches on the declared device type and
  stores the family once at discovery.
- **Ring does not report what a contact sensor is attached to.** The documentation
  tells integrators to keep that mapping application-side, which is reasonable,
  and means every integration builds the same Setup screen.
- **The Playground (May 2026) simulates Package, Vehicle and Motion camera
  events.** The release notes do not document sensor simulation, which is why
  `simulator/` exists.
- **Sensors and dynamic scopes went Early Access on 24 August 2026.** What that
  implies for publication is in the release notes rather than beside the scopes.

---

## What has and has not been run against Ring itself

**Not run against a live Ring account.** There were no Ring developer credentials
on the machine this was built on. The client, the OAuth flow, the webhook
verification and the event vocabulary are written against the documented contract
and exercised end to end against `simulator/ring_api.py`, which serves that
contract locally and signs webhooks with the documented scheme.

The demo therefore satisfies the track's "through a simulator or an actual Ring
device" requirement via the bundled simulator, and it exercises the shipping code
path rather than a stub: the 120-day seed delivers 83,800 signed webhooks through
the real receiver and makes 21,810 real client calls.

The first thing to do with credentials is the day-one spike, in this order: link
the account, `GET /v1/devices`, read the Air Quality Monitor's `/status` twice
five minutes apart and confirm `reported_at` does not move, open a contact sensor
and confirm the webhook arrives as `contact_sensor_faulted`, and drive past the
camera to confirm `motion.vehicle`. Every one of those is a claim in this
document.
