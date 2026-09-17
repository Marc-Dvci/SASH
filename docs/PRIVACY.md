# Privacy

SASH is built on a camera and never looks at what the camera sees.

That is the design constraint the whole product was built around, and this
document says exactly how it is enforced.

---

## What SASH reads

| Stream | Source | Why |
|---|---|---|
| `motion_detected` with `sub_type = vehicle`, **timestamps only** | Camera webhook | The street's traffic rhythm, which is the covariate the coupling is measured against |
| Contact transitions, open and closed | Contact sensor webhook | Which opening was open, and for how much of each interval |
| PM2.5, CO, temperature, humidity | `GET /v1/devices/{id}/status` | The measurement the whole model is fitted to |
| `device_online` / `device_offline` | Lifecycle webhook | An offline camera reports no vehicles, and that is not a quiet street |
| `pm25_exceeded` / `pm25_cleared` and the other threshold events | AQM webhook | Shown on the timeline; they are Ring's own alert thresholds |

## What SASH never requests

- Video downloads
- Image snapshots
- Live streaming sessions (WHEP)
- Any person, face, or identity
- Anything at all about who is home or when

**Human motion is delivered and discarded at the door.** Ring's grant is per
device, not per event sub-type, so a camera scope delivers people as well as
vehicles. SASH drops every non-vehicle motion event in `EventProcessor` before
anything is written, and counts the drops. On the demo home's 120 days that is
10,300 discarded events against 72,099 kept. A record of when the household comes
and goes is exactly what this product must not accumulate, and
`tests/test_webhook.py::test_human_motion_is_discarded_at_the_door` pins it.

---

## How the media refusal is enforced

By a path allowlist in `ring/client.py`:

```python
ALLOWED_PATHS = (
    r"^/v1/devices$",
    r"^/v1/devices/[^/]+$",
    r"^/v1/devices/[^/]+/status$",
    r"^/v1/devices/[^/]+/capabilities$",
    r"^/v1/devices/[^/]+/location$",
    r"^/v1/devices/[^/]+/configurations$",
    r"^/v1/history/devices/[^/]+/events$",
    r"^/v1/users/me$",
    r"^/v1/accounts/me/app-integrations$",
    r"^/v1/accounts/me/subscriptions$",
)
```

Ten entries. Any path containing `/media/` raises `MediaAccessDenied` **before a
socket is opened**; anything else off the list raises `PathNotAllowed`. Adding to
that list is the only way to widen the client's reach, which makes the blast
radius of the Ring grant reviewable in one screen. A test asserts the list stays
under twelve entries, so the control stays short enough to read.

The three refused methods (`download_video`, `download_image`, `start_live_view`)
exist on the client on purpose, so the refusal is discoverable from the client's
own surface rather than being an absence.

**The test runs against a server that would have complied.** `simulator/ring_api.py`
implements all three media endpoints and returns a body.
`tests/test_no_media.py` first shows the simulator answering the request, then
shows the client refusing to make it and the transport call count not moving. A
refusal tested against a server that 404s proves nothing.

---

## The scope, and what SASH does with it

Ring's Cameras and Doorbells scope carries the media endpoints together with the
motion events SASH needs, so a household granting SASH access to its camera is
granting more than SASH uses. That is friction log entry 6. Everything in this
document is how SASH answers it: the allowlist, the tests, the Setup panel that
lists both columns, and the fact that the panel is in the product rather than
in a policy page.

---

## What is stored, and where

One SQLite file on the machine running SASH. Nothing leaves it.

| Table | Contents |
|---|---|
| `devices` | Ring device id, name, family, and the household's own role and orientation mapping |
| `webhook_receipts` | `request_id`, event type, device id. The idempotency ledger |
| `vehicle_events` | Camera id and a timestamp. No sub-type detail, no media reference |
| `contact_events` | Device id, timestamp, `open` or `closed` |
| `aq_samples` | Device id, `reported_at`, `fetched_at`, and the four channel values |
| `aq_threshold_events` | Ring's own alert crossings |
| `device_health` | Online and offline transitions |
| `plans` | What SASH recommended, what it forecast, and what the readings then showed |
| `meta` | Configuration, and the OAuth tokens, obfuscated at rest |

There is no user table, no location beyond what Ring reports for the device, and
no analytics or telemetry of any kind. SASH makes no outbound request to anything
except the Ring API, and, only if `BEDROCK_MODEL_ID` is set, to Amazon Bedrock.

---

## The Bedrock adapter, if it is switched on

Off by default. When on, it is sent the **fact dictionary** and nothing else: the
opening's name, a start time, a duration, two forecast numbers, a percentage and
the reasons airing is due. It is never sent readings, event times, contact
history, or anything about the household. It is asked to phrase a sentence, never
to decide one, and if it fails or is slow the deterministic text ships. The
numbers on screen are identical either way.

---

## What a household controls

- **The role mapping.** Ring reports a contact sensor's state, never what it is
  attached to. The Setup tab is where a household says which sensor is on which
  opening and which way it faces, and it can set any device to "not used by SASH",
  which removes it from the model immediately.
- **The plan.** SASH does not open windows and has no actuator. Every
  recommendation is a sentence on a screen, and a plan nobody acts on is recorded
  as exactly that.
- **The record.** One file. Deleting it deletes everything SASH knows.
