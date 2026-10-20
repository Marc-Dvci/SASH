# Friction log

Twelve entries from building SASH against the Ring Partner API and its
documentation, in the order they cost time. Each one is a task I attempted, what
I expected, what happened, what I did instead, and what I would change.

The context matters for reading these: SASH is a **sensor-first** integration.
It reads contact sensors and an Air Quality Monitor continuously and treats the
camera as a source of event metadata only, never media. Most of the friction
below comes from that direction of travel, which the tooling was shaped for
later than the camera path.

---

## 1. A sensor integration starts blind

**Task.** On first link, backfill enough history to fit a model, so a household
sees something useful on day one rather than in week six.

**Steps.** Used `GET /v1/history/devices/{id}/events` for the camera, then read
the Sensors section for the equivalent.

**Expected.** History for sensors, or camera history reaching back before the
link.

**Actual.** The Sensors section states that webhook delivery is the only source
of sensor event data, and the Event History reference states that camera
results are time-gated to events after the household granted consent. So a new
integration's record starts at link time on both surfaces. Measured on 24
simulated homes left to their habit, SASH reached a first plan after a median
of 18 days and had every window measured after 40, and all of that wait is
this.

**Severity.** High. It sets the time-to-value of every sensor integration.

**Workaround.** SASH turns the wait into a calibration. From the first day it
asks for the airings its reporting gate needs, one window at a time, with
progress ("After you cook, open the Courtyard window for 30 minutes with the
other windows shut. 2 of 6 readings after cooking"), and `sash live` polls from
the first minute. On the same 24 homes, following those steps with probability
0.6 brought the medians to 9 days for a first plan and 11 for every window
(`bench/onboarding.py`).

**Suggestion.** Extend `/v1/history/devices/{id}/events` to sensor device
families, and let the consent screen offer the household the choice of sharing
history before the link date. Thirty days of contact transitions would change
what a sensor integration can do on the day it is installed.

---

## 2. The averaging window of an Air Quality Monitor reading is undocumented

**Task.** Align a PM2.5 reading with the vehicle-motion events and contact state
that explain it.

**Steps.** Read the Sensors reference and the status reference looking for
whether the reported value is instantaneous or an average over the preceding
reporting period.

**Expected.** One sentence, or a field, saying which.

**Actual.** Neither. The two readings imply covariate alignments half a
reporting period apart, and nothing in the payload says which one is right.

**Severity.** High, and this is the single most consequential undocumented fact
in the API for this class of application. On the simulator, assuming a trailing
average when the value is instantaneous moved a recovered air exchange rate from
4.20 air changes per hour to 0.20. That is a different answer, and nothing in
the data would have flagged it.

**Workaround.** `intervals.SAMPLE_LAG_MIN` is an explicit, documented assumption
with a knob on it, and `LAG_CANDIDATES` exists so a household's own contact
transitions can be used to pick the alignment that best explains its own
readings.

**Suggestion.** State it in the Air Quality Monitor reference, and ideally add
`measurement_window_seconds` to the status payload so an integration can read it
rather than assume it.

---

## 3. The carbon monoxide reading has no unit

**Task.** Use the CO channel as a decaying tracer.

**Steps.** Read "Reading attributes by device": temperature is degrees Celsius,
humidity a relative percentage, PM2.5 µg/m³, all floats.

**Expected.** A unit and a resolution for `co`.

**Actual.** `co` is listed with the others and its unit is not stated. The
product material's alert threshold is 9 ppm, which implies ppm, and the
resolution is not stated anywhere. The tracer method depends on both: a decay
from 1.4 to 0.1 ppm is a measurement at 0.01 ppm resolution and is invisible
at 1 ppm.

**Severity.** Medium to high for anything that models the CO channel rather
than alarming on it.

**Workaround.** SASH assumes ppm, matching the alert threshold, and
`docs/MODEL.md` states the resolution the tracer needs so the assumption is
visible.

**Suggestion.** Add the unit and the reporting resolution of `co` to the
reading attributes table, beside the ones the other three channels have.

---

## 4. Device status returns the last reported reading, and the cadence is not in the endpoint docs

**Task.** Read the current indoor PM2.5.

**Steps.** Called `GET /v1/devices/{id}/status` on a schedule.

**Expected.** A documented cadence next to the endpoint so I could set the
schedule.

**Actual.** The reference says the value is the last reported reading and can
lag by up to an hour, which is clear. The Air Quality Monitor's fifteen-minute
reporting cadence is in the product material rather than in the endpoint
reference, so the first version of my poller ran every minute and stored
fourteen copies of every measurement. Since the model is a mass balance over
consecutive readings, that flattened every decay in the record and made the home
look far leakier than it is.

**Severity.** Medium to high. It corrupts a model rather than breaking a call.

**Workaround.** `reported_at` is part of the primary key of the sample table, so
a repeat is refused and counted as a health signal instead of stored as a
measurement. `tests/test_store_and_ingest.py` pins six polls across one
reporting slot producing exactly one sample.

**Suggestion.** Put the reporting cadence in the status endpoint reference, per
device family. An explicit `age_seconds` would be better still.

---

## 5. Sensor events are outside the Playground

**Task.** Get a contact-sensor open and close event, and an Air Quality Monitor
reading, into a local webhook receiver before writing any analysis.

**Steps.** Read the Playground release notes and the Sensors reference looking
for a way to fire a sensor event or set a sensor's reported status.

**Expected.** The Playground simulates events for the device families the API
exposes, so a sensor integration could be developed the same way a camera
integration can.

**Actual.** The Playground simulates Package, Vehicle and Motion camera events
and issues a token without an app, which is excellent for the camera half. The
sensor families that went Early Access on 24 August have no simulated events.

**Severity.** High. It is the difference between an afternoon and a week for
anyone building on the sensor scopes.

**Workaround.** I wrote `simulator/`, a Ring account simulator that serves the
documented endpoints as JSON:API compound documents and signs webhooks with the
documented HMAC scheme. It is about 1,500 lines and most of a day. It became the
most valuable component in the project, and `tests/test_ring_contract.py` keeps
it aligned with the reference pages' own example payloads.

**Suggestion.** Add contact-sensor faulted/cleared and Air Quality Monitor
threshold events to the Playground's event menu, and a way to set a device's
reported status. Even a single button per sensor event type would remove the
need for anyone to write what I wrote.

---

## 6. The camera scope is broader than a metadata-only integration needs

**Task.** Subscribe to `motion.vehicle` events without acquiring the ability to
read video, snapshots or a live stream.

**Steps.** Read the scope list looking for an events-only camera scope.

**Expected.** Something like `camera.events.read` separate from
`camera.media.read`.

**Actual.** The Cameras and Doorbells scope that delivers motion events also
carries the media endpoints. SASH never fetches an image and does not want the
ability to, and a household granting the scope cannot tell that from the consent
screen.

**Severity.** Medium. It is a trust problem rather than a technical one, and
trust is the whole product in a category like this.

**Workaround.** The client carries a path allowlist and raises
`MediaAccessDenied` before a socket is opened. `tests/test_no_media.py` walks
every documented media path against a simulator that *would* have answered, so
the refusal is tested rather than asserted. The UI has a panel listing what SASH
reads and what it never requests.

**Suggestion.** Split media out of the camera scope, or add a partner-declared
"metadata only" attestation that the consent screen can show. Every integration
that only needs event metadata is currently asking for a household's cameras.

---

## 7. `contact_sensor_faulted` means the window is open

**Task.** Turn contact events into open and closed state.

**Steps.** Read the sensor event reference and the status reference.

**Expected.** Something with `open` and `closed` in the name, or a `state` field
on the event.

**Actual.** The status reference carries a callout saying `faulted: true` is the
active condition and that the name is inherited from the underlying protocol,
which is exactly the right callout. The event reference lists
`contact_sensor_faulted` and `contact_sensor_cleared` with no attribute
carrying the new state, so the event type is the entire signal and every
integration re-derives the same mapping. Getting it backwards inverts every
downstream estimate without raising anything.

**Severity.** Medium, because the failure mode is silent.

**Workaround.** One function, `events.contact_state()`, and one test that pins
it. Nothing else in SASH is allowed to touch the event names.

**Suggestion.** Add a plain `state: "open" | "closed"` attribute to the contact
events alongside the alarm vocabulary, and repeat the status callout in the
event reference.

---

## 8. PM2.5 has two names

**Task.** One parser for particulate, across status and events.

**Steps.** Read the status reference (`pm2_5`) and the threshold event
reference (`pm25_level`).

**Expected.** One name.

**Actual.** Two, and the reference warns about it, which is why it cost an hour
rather than a day.

**Severity.** Low.

**Workaround.** The poller reads `pm2_5`; the threshold handler reads
`pm25_level`; each is pinned by a test with the documented example.

**Suggestion.** Accept both names on both surfaces for a deprecation window,
then keep one.

---

## 9. There is no device family on the device resource

**Task.** Decide, from a `/v1/devices` response, which device is a camera, which
is a contact sensor and which is the Air Quality Monitor.

**Steps.** Read the device resource (`type: "devices"`, `attributes.name`,
`image_url`), then the capabilities and status resources.

**Expected.** A family field to switch on.

**Actual.** The documentation says not to infer the family from capabilities,
because the keys are shared across families. What remains is the status
attribute set: a device reporting `pm2_5` is the Air Quality Monitor, one
reporting `contact_detection` is a contact sensor, one declaring `video` is a
camera. Every integration writes that classifier.

**Severity.** Low. It is documented and it only cost an hour.

**Workaround.** `ingest._family_of` reads the sideloaded status first, the
capabilities second, and stores the family once at discovery. The role a device
plays in the home is the household's, on the Setup tab.

**Suggestion.** A stable `device_family` attribute on the device resource,
documented as the field to switch on.

---

## 10. A device that has not checked in returns placeholders

**Task.** Poll a newly added Air Quality Monitor.

**Steps.** Read the common sensor telemetry section.

**Expected.** An error, or an empty status, for a device that has never
reported.

**Actual.** The reference warns that `reported_at` returns `null` or the Unix
epoch, that `battery_status` and `signal_strength` are absent, and that the Air
Quality Monitor reports `battery_status.percentage: 255` because it is mains
powered. The warning is precise and it means three guards in every client.

**Severity.** Low, because the warning exists.

**Workaround.** `events.epoch_seconds` returns nothing for `null`, the epoch
and an empty string, and the poller counts such a poll as a failure rather than
storing 1970 as a reading time. `tests/test_ring_contract.py` pins all four
placeholder forms.

**Suggestion.** Omit `reported_at` on a device that has never reported, and
omit `battery_status` on a device without a battery, so absence carries the
meaning rather than a sentinel.

---

## 11. Ring does not report what a contact sensor is attached to

**Task.** Know which opening a contact sensor is on, and which way it faces.

**Steps.** Called `/v1/devices/{id}`, `/capabilities`, `/location` and
`/configurations` looking for anything about placement.

**Expected.** At least the name the household gave the device in the Ring app.

**Actual.** The device's own name comes through, which helps, and the
documentation tells integrators to keep the placement mapping
application-side, which is a reasonable position.

**Severity.** Low to medium. It is reasonable and it is re-implemented by every
integration that needs it.

**Workaround.** `sash live` assigns each discovered device the role its family
implies, and the Setup tab is where a household says which side each opening
faces.

**Suggestion.** Since every integration builds this, a small standard would help:
an optional partner-writable `placement` key on the device, so the mapping
survives an integration being replaced.

---

## 12. Early Access publication limits sit in a banner on one page

**Task.** Understand what publishing a sensor-scope integration would involve.

**Steps.** Read the certification and publication material after the integration
worked.

**Expected.** The path to publication stated alongside the scopes.

**Actual.** The Sensors section's Early Access banner says sensor API calls can
ship in an app and that publication waits on dynamic scopes reaching general
availability. The scope reference itself does not repeat it, so a developer
reading scopes first builds before learning what the integration can become.

**Severity.** Low for a hackathon, higher for anyone deciding what to invest in.

**Workaround.** None needed here.

**Suggestion.** Repeat the Early Access status and its publication implication on
the scope reference page.

---

## What worked well, so this reads fairly

The webhook envelope is clean and `meta.request_id` is exactly the right
idempotency key; SASH deduplicates on it and the count of absorbed retries is a
health signal. The reference is unusually candid about its own traps: raw-body
signing, the `faulted` vocabulary, the two PM2.5 names and the placeholder
values each have a callout, and every one of those callouts saved me time. The
rate-limit headers are complete enough to build backoff against without
guessing. The OAuth flow is standard and had no surprises. The Playground's
one-click token means the first call to a Ring account needs no app
registration at all. And the decision to expose vehicle classification as **event metadata** is the
reason this project exists at all: SASH gets a usable street signal without ever
fetching a frame, which is a privacy property most camera-derived signals cannot
offer.
