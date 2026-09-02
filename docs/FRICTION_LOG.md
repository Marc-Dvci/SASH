# Friction log

Ten entries from building SASH against the Ring Partner API and its
documentation, in the order they cost time. Each one is a task I attempted, what
I expected, what happened, what I did instead, and what I would change.

The context matters for reading these: SASH is a **sensor-first** integration.
It reads contact sensors and an Air Quality Monitor continuously and treats the
camera as a source of event metadata only, never media. Most of the friction
below comes from that direction of travel, which the tooling is not yet shaped
for.

---

## 1. A sensor integration cannot be developed against the Playground

**Task.** Get a contact-sensor open and close event, and an Air Quality Monitor
reading, into a local webhook receiver before writing any analysis.

**Steps.** Read the Playground release notes, requested a one-click token, looked
for a way to fire a sensor event.

**Expected.** The Playground simulates events for the device families the API
exposes, so a sensor integration could be developed the same way a camera
integration can.

**Actual.** The Playground simulates Package, Vehicle and Motion camera events.
The release notes do not document sensor simulation, and I could not find a way
to make a contact sensor fault or an Air Quality Monitor report.

**Severity.** High. It is the difference between an afternoon and a week for
anyone building on the sensor scopes that went Early Access in August.

**Workaround.** I wrote `simulator/` — a Ring account simulator that serves the
documented endpoints and signs webhooks with the documented HMAC scheme. It is
about 1,400 lines and most of a day. It turned out to be the most valuable
component in the project, but building it was not the project.

**Suggestion.** Add contact-sensor faulted/cleared and Air Quality Monitor
threshold events to the Playground's event menu, and a way to set a device's
reported status. Even a single button per sensor event type would remove the
need for anyone to write what I wrote.

---

## 2. There is no event-history endpoint for sensors

**Task.** On first link, backfill enough history to fit a model, so a household
sees something useful on day one rather than in week six.

**Steps.** Used `GET /v1/history/devices/{id}/events` for the camera, then looked
for the sensor equivalent.

**Expected.** A symmetric history endpoint, or at least a documented statement
that there is not one.

**Actual.** History is available for camera events. For sensors, webhooks are the
only source, so a new integration's record starts at link time and is blind for
as long as it takes to accumulate airings. SASH needs roughly 30 to 60 days
before it will report an opening at all, and the whole of that wait is a product
problem created by this gap.

**Severity.** High. It sets the time-to-value of every sensor integration.

**Workaround.** SASH shows each opening's reason for not being reported yet
("this opening needs 6 more airings with the carbon-monoxide channel above its
floor") rather than a spinner, and backfills camera history where it can. There
is nothing to work around on the sensor side.

**Suggestion.** Extend `/v1/history/devices/{id}/events` to sensor device
families, even with a shorter retention window than camera events. Thirty days
of contact transitions would change what a sensor integration can do on the day
it is installed.

---

## 3. The averaging window of an Air Quality Monitor reading is undocumented

**Task.** Align a PM2.5 reading with the vehicle-motion events and contact state
that explain it.

**Steps.** Read the Air Quality Monitor reference and the device status reference
looking for whether the reported value is instantaneous or an average over the
preceding reporting period.

**Expected.** One sentence, or a field, saying which.

**Actual.** Neither. The two readings imply covariate alignments half a
reporting period apart, and there is no way to tell from the payload which one
is right.

**Severity.** High, and this is the single most consequential undocumented fact
in the API for this class of application. On the simulator, assuming a trailing
average when the value is instantaneous moved a recovered air exchange rate from
4.20 air changes per hour to 0.20. That is not a degraded estimate, it is a
different answer, and nothing in the data would have told me it was wrong.

**Workaround.** `intervals.SAMPLE_LAG_MIN` is an explicit, documented assumption
with a knob on it, and `LAG_CANDIDATES` exists so a household's own contact
transitions can be used to pick the alignment that best explains its own
readings.

**Suggestion.** State it in the Air Quality Monitor reference, and ideally add
`measurement_window_seconds` to the status payload so an integration can read it
rather than assume it.

---

## 4. `contact_sensor_faulted` means the window is open

**Task.** Turn contact events into open and closed state.

**Steps.** Read the sensor event reference, mapped `faulted` to a fault
condition and `cleared` to normal, wrote the ingest path, looked at the results.

**Expected.** Something with `open` and `closed` in the name, or a `state` field.

**Actual.** `contact_sensor_faulted` is the magnet and reed switch being apart,
which is the window being **open**. `contact_sensor_cleared` is closed. The
vocabulary is correct from the alarm panel's point of view and inverted from the
application's.

**Severity.** Medium, but the failure mode is severe: it inverts every downstream
estimate silently. Nothing raises, no count looks wrong, and the model happily
fits a home whose windows are shut whenever they are open.

**Workaround.** One function, `events.contact_state()`, and one test that pins
it. Nothing else in SASH is allowed to touch the event names.

**Suggestion.** Add a callout box to the sensor event reference. Better, add a
plain `state: "open" | "closed"` attribute to the payload alongside the alarm
vocabulary, so integrations do not each re-derive the mapping.

---

## 5. Device status returns the last reported reading, and the cadence is not in the endpoint docs

**Task.** Read the current indoor PM2.5.

**Steps.** Called `GET /v1/devices/{id}/status` on a schedule.

**Expected.** Either a live reading, or a documented cadence next to the endpoint
so I could set the schedule.

**Actual.** The endpoint returns the last reported reading. The Air Quality
Monitor's roughly fifteen-minute reporting cadence is documented in the product
material rather than in the endpoint reference, so the first version of my
poller ran every minute and stored fourteen copies of every measurement. Since
the model is a mass balance over consecutive readings, that flattened every
decay in the record and made the home look far leakier than it is.

**Severity.** Medium to high. It is silent, and it corrupts a model rather than
breaking a call.

**Workaround.** `reported_at` is the primary key of the sample table, so a repeat
is refused and counted as a health signal instead of stored as a measurement.
`tests/test_store_and_ingest.py` pins six polls across one reporting slot
producing exactly one sample.

**Suggestion.** Put the reporting cadence in the status endpoint reference, per
device family, and say plainly that the value is the last reported one. A
`stale: true` flag, or an explicit `age_seconds`, would be better still.

---

## 6. The camera scope is broader than a metadata-only integration needs

**Task.** Subscribe to `motion.vehicle` events without acquiring the ability to
read video, snapshots or a live stream.

**Steps.** Read the scope list looking for an events-only camera scope.

**Expected.** Something like `camera.events.read` separate from
`camera.media.read`.

**Actual.** The Cameras and Doorbells scope that delivers motion events also
carries the media endpoints. SASH never fetches an image and does not want the
ability to, but a household granting it cannot tell that from the consent screen.

**Severity.** Medium. It is a trust problem rather than a technical one, and
trust is the whole product in a category like this.

**Workaround.** The client carries a path allowlist and raises
`MediaAccessDenied` before a socket is opened. `tests/test_no_media.py` walks
every documented media path against a simulator that *would* have answered, so
the refusal is tested rather than asserted. The UI has a panel listing what SASH
reads and what it never requests. All of this is compensation for a scope I
could not decline.

**Suggestion.** Split media out of the camera scope, or add a partner-declared
"metadata only" attestation that the consent screen can show. Every integration
that only needs event metadata is currently asking for a household's cameras.

---

## 7. Ring does not report what a contact sensor is attached to

**Task.** Know which opening a contact sensor is on, and which way it faces.

**Steps.** Called `/v1/devices/{id}`, `/capabilities`, `/location` and
`/configurations` looking for anything about placement.

**Expected.** At least the name the household gave the device in the Ring app.

**Actual.** The device's own name comes through, which helps, but nothing about
placement or orientation. The documentation tells integrators to keep that
mapping application-side, which is a reasonable position.

**Severity.** Low to medium. It is reasonable and it is also re-implemented by
every integration that needs it.

**Workaround.** A Setup tab where a household maps each device to a role and a
side, stored in SASH.

**Suggestion.** Since every integration builds this, a small standard would help:
an optional partner-writable `placement` key on the device, or a documented
convention for a `location` sub-resource, so the mapping survives an integration
being replaced.

---

## 8. Capability keys are shared across device families

**Task.** Decide, from a `/v1/devices` response, which device is a camera, which
is a contact sensor and which is the Air Quality Monitor.

**Steps.** Tried to key off the capabilities block.

**Expected.** Capability keys distinctive enough to identify a family.

**Actual.** The documentation warns that capability keys are shared across
families, so a capability probe is not a reliable family test. Guessing from
capabilities looks like it works right up until a household owns a device I did
not anticipate.

**Severity.** Low. It is documented, which is why it only cost an hour.

**Workaround.** `ingest._family_of` uses the declared device type and treats
capabilities as advisory, and the family is stored once at discovery rather than
re-derived.

**Suggestion.** A stable `device_family` enum on the device resource, documented
as the field to switch on.

---

## 9. Webhook signature verification has a re-serialisation trap

**Task.** Verify `X-Signature`.

**Steps.** Parsed the JSON body in the framework's request handler, then computed
the HMAC over the parsed object.

**Expected.** The documented scheme to verify.

**Actual.** It did not, because re-serialising parsed JSON changes key order and
whitespace. This is my mistake rather than Ring's, and it is also the single most
common webhook bug in existence, which is why documentation is the right place to
prevent it.

**Severity.** Low, and entirely avoidable with one sentence.

**Workaround.** Verify over `await request.body()`, before parsing. A test pins
that the digest does not survive a re-serialise.

**Suggestion.** Add "compute the HMAC over the raw request body, not over
re-serialised JSON" to the webhook reference, with a one-line example in each
language sample.

---

## 10. Early Access publication limits are not visible until late

**Task.** Understand what publishing a sensor-scope integration would involve.

**Steps.** Read the certification and publication material after the integration
worked.

**Expected.** The path to publication stated alongside the scopes.

**Actual.** Sensors and dynamic scopes went Early Access on 24 August 2026, and
what that implies for publication is not stated next to the scopes themselves. A
developer can build a complete integration before learning what it can and cannot
become.

**Severity.** Low for a hackathon, high for anyone deciding what to invest in.

**Workaround.** None needed here.

**Suggestion.** Put the Early Access status and its publication implications in a
banner on the scope reference page, not only in the release notes.

---

## What worked well, so this reads fairly

The webhook envelope is clean and the `meta.request_id` idempotency key is
exactly right; SASH deduplicates on it and the count of absorbed retries is a
health signal rather than a bug. The rate-limit headers are complete enough to
build backoff against without guessing. The OAuth flow is standard and had no
surprises. And the decision to expose vehicle classification as **event
metadata** is the reason this project exists at all: SASH gets a usable street
signal without ever fetching a frame, which is a privacy property most
camera-derived signals cannot offer.
