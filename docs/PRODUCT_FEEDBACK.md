# Product feedback

The submission form's own questions, answered.

---

## Which developer tools, APIs and SDKs did you use, and for what?

**Ring Partner API (REST).** The whole product. `GET /v1/devices` with
`include=status,capabilities,location,configurations` for discovery;
`GET /v1/devices/{id}/status` polled for the Air Quality Monitor's PM2.5, CO,
temperature and humidity; `GET /v1/devices/{id}/configurations` for change
detection; `GET /v1/history/devices/{id}/events` to backfill camera history at
link time; `GET /v1/users/me` for account identity. Implemented in
`src/sash/ring/client.py`, which also carries the path allowlist that refuses
every media endpoint.

**Ring webhooks.** The only source of sensor data. `motion_detected` with
`sub_type = vehicle` from an outdoor camera gives the street's traffic rhythm;
`contact_sensor_faulted` and `contact_sensor_cleared` give which opening was
open; `device_online` / `device_offline` mark the intervals where the street
cannot be measured; the PM2.5 and CO threshold events go on the timeline.
HMAC-SHA256 over the raw body, `meta.request_id` as the idempotency key, in
`src/sash/ring/webhook.py`.

**Ring OAuth.** Authorization-code exchange and refresh against
`oauth.ring.com/oauth/token`, with the Playground's one-click token supported by
the same code path (`ring/auth.py:StaticTokenService`).

**Ring Developer documentation and the Playground.** Read closely. The Playground
issued a token; it could not simulate the sensor events this project is built on,
which is why `simulator/` exists.

**AWS (mini challenge).** Amazon Bedrock for the wording of the decision card,
behind an interface whose default is a deterministic renderer
(`src/sash/narrative.py`). A SAM stack in `infra/template.yaml`: API Gateway,
Lambda, SQS, DynamoDB, EventBridge Scheduler, Secrets Manager with a KMS key,
CloudWatch alarms, SNS. Four Lambda handlers and a DynamoDB storage adapter in
`src/sash/aws/`, with 18 tests. **Not deployed** — the AWS credentials on the
build machine are invalid, and `docs/AWS.md` says so plainly rather than
implying otherwise.

**Everything else** is Python 3.10, FastAPI, httpx, numpy and pytest. No ML
framework and no model file; the estimator is a mass balance and two regressions.

---

## What worked well?

**The webhook envelope and `meta.request_id`.** Clean, and the idempotency key is
exactly the right thing to expose. SASH deduplicates on it and reports the count
of absorbed retries as a health signal. On a 120-day replay: 83,800 deliveries,
167 duplicates absorbed, 0 rejected.

**Vehicle classification as event metadata.** This is the decision that makes the
whole project possible. Ring tells an integration that a *vehicle* moved, with a
timestamp, without the integration ever fetching a frame. That gives a usable
street-activity signal with a privacy property that a camera-derived signal
normally cannot offer, and it is the reason SASH can honestly say it never looks
at an image.

**The Air Quality Monitor's channel set.** Four channels in one device, and the
one that turned out to matter was the one I did not expect. Carbon monoxide has
no street term in it, so between cooking events it is a decaying tracer and its
decay rate measures the ventilation rate of whichever opening is ajar. That is
tracer-gas decay, the building-science standard, and Ring ships the tracer and
the meter in one box for a consumer price.

**Rate-limit headers.** `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
`Retry-After` on the 429. Complete enough to build backoff against without
guessing, which is rarer than it should be.

**OAuth.** Standard, and it had no surprises at all.

---

## What needs work?

Ten entries with severities, steps and suggestions are in
`docs/FRICTION_LOG.md`. The four that shaped this project:

1. **A sensor integration cannot be developed against the Playground.** It
   simulates Package, Vehicle and Motion camera events; the release notes do not
   document sensor simulation. I could not fire a contact-sensor fault or make an
   Air Quality Monitor report. So I wrote a Ring account simulator, about 1,400
   lines and most of a day, before I could write a line of the product. It became
   the most valuable component in the repo, and building it was not the project.

2. **There is no event-history endpoint for sensors.** Camera events have
   `/v1/history/devices/{id}/events`. Sensors do not, so webhooks are the only
   source and a sensor integration's record starts at link time. SASH needs 30 to
   60 days of a household's airings before it will report an opening, and every
   day of that wait exists because of this gap. It sets the time-to-value of the
   entire sensor scope.

3. **The averaging window of an Air Quality Monitor reading is undocumented.**
   Instantaneous, or a mean over the preceding cadence? The two imply covariate
   alignments half a reporting period apart. Measured on the simulator, the wrong
   choice moved a recovered air exchange rate from 4.20 air changes per hour to
   0.20. Not a degraded estimate: a different answer, with nothing in the data to
   say it was wrong. This is the single most consequential undocumented fact in
   the API for any application that models a room.

4. **The camera scope is broader than a metadata-only integration needs.** The
   Cameras and Doorbells scope that delivers motion events also carries video
   download, snapshot download and live streaming. SASH does not want that
   ability, and a household granting the scope cannot tell from the consent
   screen. Everything in `docs/PRIVACY.md` is compensation for a scope I could
   not decline.

Also: `contact_sensor_faulted` means the window is **open**, which is right from
the alarm panel's point of view and inverted from the application's, and gets
shipped backwards silently; device status returns the last reported reading with
the cadence documented in the product material rather than beside the endpoint;
and capability keys are shared across device families so a capability probe is
not a family test.

---

## How was your onboarding experience, from zero to hello world?

Two halves, and they were very different.

**Reading the API: good.** The documentation is well organised, the endpoint
reference is complete enough to write a client against, and the webhook contract
is stated clearly. I had a client, an OAuth flow and a signature verifier written
from the docs alone in a few hours, and they have not needed changing since.

**Getting a first event: not possible for this project.** Zero to hello world for
a *camera* integration is quick, because the Playground fires camera events. Zero
to hello world for a *sensor* integration was a day of writing a simulator,
because nothing available fires a contact-sensor fault or an Air Quality Monitor
reading. The sensor scopes went Early Access on 24 August 2026 and the tooling
has not caught up with them yet.

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
sensor simulation. Right now a sensor integration is blind on day one and
undevelopable on day zero, and both of those are tooling rather than platform.
