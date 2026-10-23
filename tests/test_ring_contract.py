"""The shapes in Ring's own reference, fed to the shipping parsers verbatim.

Each payload below is copied from the Ring Partner API documentation (Device
Discovery, Sensors, Motion Detection). The simulator is a second implementation
of the same contract, so these tests are what keep the two from agreeing with
each other and disagreeing with Ring.
"""

from __future__ import annotations

import asyncio

import pytest

from sash.ingest import AirQualityPoller, IngestStats, _family_of, discover_devices
from sash.ring import events as ev
from sash.ring.client import sideload
from sash.store import Store


# Device Discovery, "Response with Included Data (Compound Document)", with the
# sensor status attributes from the Sensors section.
COMPOUND_DOCUMENT = {
    "meta": {"time": "2026-02-14T22:57:16Z"},
    "data": [
        {
            "type": "devices",
            "id": "ava1.ring.device.XXXYYY",
            "attributes": {"name": "Office"},
            "relationships": {
                "status": {
                    "data": {"type": "device-status", "id": "ava1.ring.device.status.XXXYYY"},
                    "links": {"related": "/v1/devices/ava1.ring.device.XXXYYY/status"},
                },
                "capabilities": {
                    "data": {
                        "type": "device-capabilities",
                        "id": "ava1.ring.device.capabilities.XXXYYY",
                    },
                    "links": {"related": "/v1/devices/ava1.ring.device.XXXYYY/capabilities"},
                },
            },
        },
        {
            "type": "devices",
            "id": "ava1.ring.device.AQM001",
            "attributes": {"name": "Living Room Air"},
            "relationships": {
                "status": {
                    "data": {"type": "device-status", "id": "ava1.ring.device.status.AQM001"},
                    "links": {"related": "/v1/devices/ava1.ring.device.AQM001/status"},
                }
            },
        },
        {
            "type": "devices",
            "id": "ava1.ring.device.CS001",
            "attributes": {"name": "Kitchen window"},
            "relationships": {
                "status": {
                    "data": {"type": "device-status", "id": "ava1.ring.device.status.CS001"},
                    "links": {"related": "/v1/devices/ava1.ring.device.CS001/status"},
                }
            },
        },
    ],
    "included": [
        {
            "type": "device-status",
            "id": "ava1.ring.device.status.XXXYYY",
            "attributes": {"online": True},
        },
        {
            "type": "device-capabilities",
            "id": "ava1.ring.device.capabilities.XXXYYY",
            "attributes": {
                "video": {
                    "configurations": ["resolution_mode"],
                    "codecs": ["AVC"],
                    "ratio": "16:9",
                    "max_resolution": 1080,
                    "supported_resolutions": [1080],
                },
                "motion_detection": {"configurations": ["enabled", "motion_zones"]},
                "image_enhancements": {
                    "configurations": ["color_night_vision", "hdr", "ir_led_night_vision", "privacy_zones"]
                },
            },
        },
        {
            "type": "device-status",
            "id": "ava1.ring.device.status.AQM001",
            "attributes": {
                "online": True,
                "reported_at": "2026-03-10T14:20:05.118Z",
                "signal_strength": {"value": "good"},
                "battery_status": {"percentage": 255},
                "temperature": 24.2,
                "humidity": 44.3,
                "pm2_5": 3.5,
                "co": 0.0,
            },
        },
        {
            "type": "device-status",
            "id": "ava1.ring.device.status.CS001",
            "attributes": {
                "online": False,
                "reported_at": "2026-03-10T14:20:05.118Z",
                "battery_status": {"percentage": 85},
                "sensor_reporting_state": {"value": "active"},
                "contact_detection": {"faulted": True},
                "tamper_detection": {"detected": False},
            },
        },
    ],
}

# Sensors, "Common sensor telemetry" plus "Reading attributes by device".
AQM_STATUS = {
    "data": {
        "type": "device-status",
        "id": "ava1.ring.device.status.AQM001",
        "attributes": {
            "online": True,
            "reported_at": "2026-03-10T14:20:05.118Z",
            "signal_strength": {"value": "good"},
            "battery_status": {"percentage": 255},
            "temperature": 24.2,
            "humidity": 44.3,
            "pm2_5": 3.5,
            "co": 0.0,
        },
    }
}

# Sensors, "Sensor webhook payload".
CONTACT_WEBHOOK = {
    "meta": {
        "version": "1.1",
        "time": "2026-03-10T14:22:20.258000314Z",
        "request_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx-1773152538431-contact_senso",
        "account_id": "ava1.ring.account.XXXXXXXX",
    },
    "data": {
        "id": "ava1.ring.device.CS001_contact_sensor_faulted_1773152538431",
        "type": "contact_sensor_faulted",
        "attributes": {
            "source": "ava1.ring.device.CS001",
            "source_type": "devices",
            "timestamp": 1773152538431,
        },
        "relationships": {"devices": {"links": {"self": "/v1/devices/ava1.ring.device.CS001"}}},
    },
}

# Motion Detection, "Webhook Payload".
MOTION_WEBHOOK = {
    "meta": {
        "version": "1.1",
        "time": "2026-02-13T13:39:57.200155525Z",
        "request_id": "2ad45ade-1818-4154-813b-afdd8bcd8085",
        "account_id": "ava1.ring.account.XXXYYY",
    },
    "data": {
        "id": "ava1.ring.device.XXXYYY_vehicle_1786715596787",
        "type": "motion_detected",
        "attributes": {
            "source": "ava1.ring.device.XXXYYY",
            "source_type": "devices",
            "timestamp": 1786715596787,
            "timestamp_readable": "2026-08-14 08:53:16",
            "sub_type": "vehicle",
            "component_ids": ["0"],
        },
        "relationships": {"devices": {"links": {"self": "/v1/devices/ava1.ring.device.XXXYYY"}}},
    },
}


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def list_devices(self):
        return sideload(self.payload["data"], self.payload.get("included", []))


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_sideload_joins_included_resources_by_type_and_id():
    devices = sideload(COMPOUND_DOCUMENT["data"], COMPOUND_DOCUMENT["included"])
    office = devices[0]["attributes"]
    assert office["name"] == "Office"
    assert office["status"] == {"online": True}
    assert "video" in office["capabilities"]
    aqm = devices[1]["attributes"]
    assert aqm["status"]["pm2_5"] == 3.5


def test_family_is_read_from_what_the_device_reports():
    devices = sideload(COMPOUND_DOCUMENT["data"], COMPOUND_DOCUMENT["included"])
    families = [_family_of(d, d["attributes"]) for d in devices]
    assert families == [ev.FAMILY_CAMERA, ev.FAMILY_AQM, ev.FAMILY_CONTACT]


def test_discovery_stores_the_documented_devices(store):
    asyncio.run(discover_devices(store, _Client(COMPOUND_DOCUMENT)))
    by_id = {d.device_id: d for d in store.devices()}
    assert by_id["ava1.ring.device.AQM001"].family == ev.FAMILY_AQM
    assert by_id["ava1.ring.device.AQM001"].ring_name == "Living Room Air"
    assert by_id["ava1.ring.device.CS001"].online is False
    assert by_id["ava1.ring.device.XXXYYY"].online is True


def test_documented_status_resource_is_stored_once(store):
    poller = AirQualityPoller(store, None, IngestStats())
    assert poller._store_status("ava1.ring.device.AQM001", AQM_STATUS["data"]) is True
    assert poller._store_status("ava1.ring.device.AQM001", AQM_STATUS["data"]) is False
    rows = store.aq_samples("ava1.ring.device.AQM001")
    assert len(rows) == 1
    assert rows[0]["reported_at"] == pytest.approx(1773152405.118, abs=1e-3)
    assert rows[0]["pm25"] == 3.5
    assert rows[0]["co"] == 0.0
    assert rows[0]["temperature"] == 24.2


@pytest.mark.parametrize("placeholder", [None, "1970-01-01T00:00:00Z", 0, ""])
def test_a_device_that_has_not_checked_in_is_not_a_reading(store, placeholder):
    poller = AirQualityPoller(store, None, IngestStats())
    status = {"attributes": {**AQM_STATUS["data"]["attributes"], "reported_at": placeholder}}
    assert poller._store_status("ava1.ring.device.AQM001", status) is False
    assert store.aq_samples("ava1.ring.device.AQM001") == []


def test_epoch_seconds_reads_every_documented_form():
    assert ev.epoch_seconds(1773152538431) == pytest.approx(1773152538.431)
    assert ev.epoch_seconds("2026-03-10T14:20:05.118Z") == pytest.approx(1773152405.118, abs=1e-3)
    assert ev.epoch_seconds("2026-02-14T22:57:16Z") == pytest.approx(1771109836.0)
    assert ev.epoch_seconds(1773152538.431) == pytest.approx(1773152538.431)


def test_documented_contact_webhook_is_an_opening():
    event = ev.parse(CONTACT_WEBHOOK, received_at=0.0)
    assert event.device_id == "ava1.ring.device.CS001"
    assert event.timestamp == pytest.approx(1773152538.431)
    assert ev.contact_state(event.event_type) == "open"
    assert event.request_id.endswith("contact_senso")


def test_documented_motion_webhook_is_a_vehicle():
    event = ev.parse(MOTION_WEBHOOK, received_at=0.0)
    assert event.is_vehicle_motion
    assert event.device_id == "ava1.ring.device.XXXYYY"
    assert event.timestamp == pytest.approx(1786715596.787)
    assert event.account_id == "ava1.ring.account.XXXYYY"


# Sensors, "Threshold event attributes": the reading rides on the event.
PM25_WEBHOOK = {
    "meta": {
        "version": "1.1",
        "time": "2026-03-10T14:22:20.258000314Z",
        "request_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx-1773152538431-pm25_exceeded",
        "account_id": "ava1.ring.account.XXXXXXXX",
    },
    "data": {
        "id": "ava1.ring.device.AQM001_pm25_exceeded_1773152538431",
        "type": "pm25_exceeded",
        "attributes": {
            "source": "ava1.ring.device.AQM001",
            "source_type": "devices",
            "timestamp": 1773152538431,
            "pm25_level": 41.2,
        },
    },
}


def test_documented_threshold_event_keeps_its_reading(store):
    from sash.ingest import EventProcessor

    EventProcessor(store).apply(ev.parse(PM25_WEBHOOK, received_at=0.0))
    (row,) = store.threshold_events()
    assert row["event_type"] == "pm25_exceeded"
    assert row["level"] == pytest.approx(41.2)


# Event History has no example in the reference. Ring's ring-api-helloworld
# sample (scripts/event_history.py) reads ``attributes.event_type`` and
# ``attributes.start``; ``start`` and ``end`` are epoch milliseconds and
# ``links.next`` carries the next page as ``page[key]``. The filter asked for
# ``motion.vehicle`` and every type came back anyway, as on the Playground.
HISTORY_NOW = 1786720000.0
HISTORY_PAGES = {
    None: {
        "data": [
            {
                "type": "history-events",
                "id": "h-vehicle",
                "attributes": {"event_type": "motion.vehicle", "start": 1786715596787, "end": 1786715608787},
            },
            {
                "type": "history-events",
                "id": "h-human",
                "attributes": {"event_type": "motion.human", "start": 1786715500000, "end": 1786715510000},
            },
            {
                "type": "history-events",
                "id": "h-motion",
                "attributes": {"event_type": "motion", "start": 1786715400000, "end": 1786715410000},
            },
            {
                "type": "history-events",
                "id": "h-on-demand",
                "attributes": {"event_type": "on_demand", "start": 1786715300000, "end": 1786715330000},
            },
        ],
        "links": {"next": "/v1/history/devices/ava1.ring.device.XXXYYY/events?page[key]=k2"},
    },
    "k2": {
        "data": [
            {
                "type": "history-events",
                "id": "h-vehicle-2",
                "attributes": {"event_type": "motion.vehicle", "start": 1786712000000, "end": 1786712012000},
            },
            {
                "type": "history-events",
                "id": "h-vehicle-old",
                "attributes": {"event_type": "motion.vehicle", "start": 1786000000000, "end": 1786000012000},
            },
        ],
        "links": {"next": "/v1/history/devices/ava1.ring.device.XXXYYY/events?page[key]=k3"},
    },
    "k3": {
        "data": [],
        "links": {"next": "/v1/history/devices/ava1.ring.device.XXXYYY/events?page[key]=k4"},
    },
}


class _HistoryTransport:
    """Serves ``HISTORY_PAGES`` over HTTP to the real client, and records each query."""

    def __init__(self):
        self.queries = []

    def __call__(self, request):
        import httpx

        params = dict(request.url.params)
        self.queries.append(params)
        return httpx.Response(200, json=HISTORY_PAGES[params.get("page[key]")])


def _history_client(transport):
    import httpx

    from sash.config import RingConfig
    from sash.ring.client import RingClient

    async def token():
        return "t"

    config = RingConfig()
    config.api_base_url = "http://ring"
    return RingClient(config, token, transport=httpx.MockTransport(transport))


def test_history_rows_are_read_from_start_in_milliseconds_and_filtered_here(store):
    from sash.ingest import backfill_vehicle_history

    transport = _HistoryTransport()
    client = _history_client(transport)
    found = asyncio.run(
        backfill_vehicle_history(store, client, "ava1.ring.device.XXXYYY", now=HISTORY_NOW)
    )
    asyncio.run(client.aclose())

    # Only motion.vehicle inside the window is stored, at the row's own start.
    assert store.vehicle_event_times() == [
        pytest.approx(1786712000.0),
        pytest.approx(1786715596.787),
    ]
    assert found.vehicle_added == 2
    assert found.outside_window == 1
    assert found.unclassified == 1
    assert found.by_type == {"motion.vehicle": 3, "motion.human": 1, "motion": 1, "on_demand": 1}
    # Pages followed by page[key] until one came back empty, the filter re-sent on each.
    assert found.pages == 3
    assert [q.get("page[key]") for q in transport.queries] == [None, "k2", "k3"]
    assert all(q["event_types"] == "motion.vehicle" for q in transport.queries)
    # No undocumented parameters.
    assert all(set(q) <= {"event_types", "page[key]"} for q in transport.queries)


def test_history_does_not_count_a_webhook_vehicle_twice(store):
    from sash.ingest import backfill_vehicle_history

    event = ev.parse(MOTION_WEBHOOK, received_at=0.0)
    store.add_vehicle_event(event.request_id, event.device_id, event.timestamp, "webhook")
    client = _history_client(_HistoryTransport())
    found = asyncio.run(
        backfill_vehicle_history(store, client, "ava1.ring.device.XXXYYY", now=HISTORY_NOW)
    )
    asyncio.run(client.aclose())
    assert found.vehicle_on_record == 1
    assert found.vehicle_added == 1
    assert store.vehicle_event_count() == 2

    # A second pass adds nothing.
    client = _history_client(_HistoryTransport())
    again = asyncio.run(
        backfill_vehicle_history(store, client, "ava1.ring.device.XXXYYY", now=HISTORY_NOW)
    )
    asyncio.run(client.aclose())
    assert again.vehicle_added == 0
    assert store.vehicle_event_count() == 2
