# Product feedback

The submission form's own questions, answered.

---

## Which developer tools, APIs and SDKs did you use, and for what?

**Ring Partner API (REST).** The whole product. `GET /v1/devices` with
`include=status,capabilities,location,configurations` for discovery, joined
from the JSON:API compound document; `GET /v1/devices/{id}/status` polled for
the Air Quality Monitor's PM2.5, CO, temperature and humidity;
`GET /v1/devices/{id}/configurations` for change detection on motion zones;
`GET /v1/history/devices/{id}/events` with `event_types=motion.vehicle` for
camera history since the link; `GET /v1/users/me` for account identity.
Implemented in `src/sash/ring/client.py`, which also carries the path allowlist
that refuses every media endpoint. `src/sash/live.py` runs discovery and the
poller against an account, from a Playground token or a linked app.

**Ring webhooks.** The only source of sensor data. `motion_detected` with
`sub_type = vehicle` from an outdoor camera gives the street's traffic rhythm;
`contact_sensor_faulted` and `contact_sensor_cleared` give which opening was
open; `device_online` / `device_offline` mark the intervals where the street
cannot be measured; the PM2.5 and CO threshold events go on the timeline.
HMAC-SHA256 over the raw body, `meta.request_id` as the idempotency key, in
`src/sash/ring/webhook.py`. `tests/test_ring_contract.py` feeds the reference
pages' own example payloads to the parsers.

**Ring OAuth.** Authorization-code exchange and refresh against
`oauth.ring.com/oauth/token` with scope `ava`, started from `/ring/link` and
completed at `/ring/callback`. The Playground's one-click token drops into the
same client through `RING_ACCESS_TOKEN`.

**Ring developer documentation and the Playground release notes.** The
reference pages are the contract the simulator serves and the tests pin.

**AWS (mini challenge).** A SAM stack deployed in `us-east-1`: API Gateway,
Lambda, SQS, DynamoDB, EventBridge Scheduler, Secrets Manager with a KMS key,
CloudWatch alarms, SNS. Four Lambda handlers and a DynamoDB storage adapter in
`src/sash/aws/`, 19 tests against doubles, and the same sequence verified
against the live stack. Amazon Bedrock phrases the decision card behind an
interface whose default is a deterministic renderer (`src/sash/narrative.py`):
`openai.gpt-oss-120b` on the Bedrock API endpoint, run against the deployed
stack's decision, 3 of 3 sentences accepted by the number guard, 2.0 to 2.5 s each.
`docs/AWS.md`.

**Everything else** is Python 3.10, FastAPI, httpx, numpy and pytest. No ML
framework and no model file; the estimator is a mass balance and two regressions.

---

## What worked well?

**The webhook envelope and `meta.request_id`.** Clean, and the idempotency key is
exactly the right thing to expose. SASH deduplicates on it and reports the count
of absorbed retries as a health signal. On a 120-day replay: 83,800 deliveries,
167 duplicates absorbed, 0 rejected. Against the deployed stack, a retry of a
real delivery came back `200 duplicate request_id, already processed`.

**Vehicle classification as event metadata.** This is the decision that makes the
whole project possible. Ring tells an integration that a *vehicle* moved, with a
timestamp, without the integration ever fetching a frame. That gives a usable
street-activity signal with a privacy property that a camera-derived signal
normally cannot offer, and it is the reason SASH never looks at an image.

**The Air Quality Monitor's channel set.** Four channels in one device, and the
one that turned out to matter was the one I did not expect. Carbon monoxide has
no street term in it, so between cooking events it is a decaying tracer and its
decay rate measures the ventilation rate of whichever opening is ajar. That is
tracer-gas decay, the building-science standard, and Ring ships the tracer and
the meter in one box for a consumer price.

**The reference's candour.** Raw-body signing, the `faulted` vocabulary, the two
PM2.5 names, the placeholder values on a device that has not checked in, the
255 battery reading on a mains device: each has its own callout, and each
callout saved me an afternoon.

**Rate-limit headers.** `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
`Retry-After` on the 429. Complete enough to build backoff against without
guessing, which is rarer than it should be.

**OAuth.** Standard, and it had no surprises at all.

---

## What needs work?

Twelve entries with severities, steps and suggestions are in
`docs/FRICTION_LOG.md`. The four that shaped this project:

1. **A sensor integration starts blind.** Webhooks are the only source of
   sensor events, and camera history is gated to the consent date, so a new
   integration's record begins at link time. SASH needs 30 to 60 days of a
   household's airings before it will report an opening, and every day of that
   wait exists because of this gap. It sets the time-to-value of the entire
   sensor scope.

2. **The averaging window of an Air Quality Monitor reading is undocumented.**
   Instantaneous, or a mean over the preceding cadence? The two imply covariate
   alignments half a reporting period apart. Measured on the simulator, the wrong
   choice moved a recovered air exchange rate from 4.20 air changes per hour to
   0.20. This is the single most consequential undocumented fact in the API for
   any application that models a room.

3. **The CO reading has no unit and no stated resolution.** Temperature,
   humidity and PM2.5 have theirs in the reading attributes table; `co` does
   not. A tracer method needs both.

4. **Sensor events are outside the Playground.** It simulates Package, Vehicle
   and Motion camera events and issues a token without an app, which is
   excellent for the camera half; the sensor families have no simulated events.
   So I wrote a Ring account simulator, about 1,500 lines and most of a day,
   before I could write a line of the product. It became the most valuable
   component in the repo.

Also: the camera scope that delivers motion metadata also carries video,
snapshots and live view, and a household cannot tell from the consent screen
that an integration uses only the metadata; the device resource has no family
field, so every integration writes the same classifier from status attributes;
and the fifteen-minute reporting cadence lives in the product material rather
than beside the status endpoint.

---

## How was your onboarding experience, from zero to hello world?

Two halves, and they were very different.

**Reading the API: good.** The documentation is well organised, the endpoint
reference is complete enough to write a client against, and the webhook contract
is stated clearly. I had a client, an OAuth flow and a signature verifier written
from the docs alone in a few hours, and the reference's own example payloads
now run as tests against them.

**Getting a first sensor event: a day.** Zero to hello world for a *camera*
integration is a click, because the Playground fires camera events and issues a
token. Zero to hello world for a *sensor* integration was a day of writing a
simulator, because nothing available fires a contact-sensor fault or an Air
Quality Monitor reading. The sensor scopes went Early Access on 24 August 2026
and the tooling is a step behind them.

The single highest-leverage fix in the whole developer experience would be adding
sensor events to the Playground's event menu. Even one button per event type.

---

## Would you build with these devices and services again?

**Yes.**

The reason is specific rather than general. Ring is close to the only consumer
platform where a developer can get, in one household and one API, a
**street-activity signal with a timestamp** and a **four-channel indoor air
measurement**, and can get the first of those without touching imagery. That
combination is what makes it possible to measure something about a home that
nobody has ever measured about a home. SASH is a ventilation adviser rather than
a security app, and it exists because of a product decision Ring made about what
to put in an event payload.

What would make the answer emphatic rather than qualified is sensor history and
sensor simulation. Right now a sensor integration is blind on day one and needs
its own simulator on day zero, and both of those are tooling rather than
platform.
