"""The HTTP surface: Ring's webhooks come in here, and the UI reads from here.

Two halves that deliberately do not touch.

The **Ring half** is ``/ring/webhook`` and ``/ring/callback``. The webhook route
does the minimum Ring's five-second acknowledgement budget allows: verify the
HMAC over the raw bytes, record the ``request_id``, hand the event to the queue,
return 200. It returns 200 for a duplicate too, because from Ring's side a
retry that is acknowledged is a retry that stops.

The **product half** is ``/api/*``, every route of which is a read of one cached
household view. Nothing under ``/api`` fits a model or touches the network; the
view is rebuilt only when new data has actually arrived.

There is no endpoint here that returns video, an image or a stream, and there is
none in the client either. See ``ring/client.py``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import narrative
from .config import CONFIG, SashConfig
from .ingest import EventProcessor, Pipeline
from .ring.webhook import WebhookReceiver
from .service import Household
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(
    store: Store,
    *,
    config: SashConfig | None = None,
    household: Household | None = None,
    on_demo_tick: Callable[..., Any] | None = None,
    on_demo_follow: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build the SASH application around an already-open store."""
    config = config or CONFIG
    household = household or Household(store)

    app = FastAPI(
        title="SASH",
        description="Street Air, Sensed at Home. Ventilation intelligence on Ring devices.",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    processor = EventProcessor(store)
    pipeline = Pipeline(processor)
    receiver = WebhookReceiver(config.ring.hmac_key, store, pipeline)

    app.state.store = store
    app.state.config = config
    app.state.household = household
    app.state.receiver = receiver
    app.state.pipeline = pipeline
    app.state.started_at = time.time()

    @app.on_event("startup")
    async def _start() -> None:
        app.state.pipeline_task = asyncio.create_task(pipeline.run())

    @app.on_event("shutdown")
    async def _stop() -> None:
        pipeline.stop()
        task = getattr(app.state, "pipeline_task", None)
        if task:
            task.cancel()

    # --- Ring ------------------------------------------------------------

    @app.post("/ring/webhook")
    async def ring_webhook(request: Request) -> Response:
        """Ring's delivery endpoint. Verify, dedupe, enqueue, acknowledge.

        Ring expects a 200 within five seconds and retries otherwise, so the
        analysis deliberately does not happen here. An invalid signature gets a
        401 rather than a 200, because a retry of an unsigned body will not help
        anyone and the count of them is worth watching.
        """
        raw = await request.body()
        outcome = receiver.handle(raw, request.headers.get("X-Signature"), time.time())
        if not outcome.accepted:
            status = 401 if "signature" in outcome.reason.lower() else 400
            return JSONResponse({"ok": False, "reason": outcome.reason}, status_code=status)
        return JSONResponse({"ok": True, "reason": outcome.reason})

    @app.get("/ring/callback")
    async def ring_callback(code: str = "", state: str = "", error: str = "") -> Response:
        """Where Ring returns the household after it authorises the link."""
        if error:
            return PlainTextResponse(f"Ring returned an error: {error}", status_code=400)
        if not code:
            return PlainTextResponse("No authorisation code in the callback.", status_code=400)
        store.set_meta("pending_authorization_code", code)
        store.set_meta("pending_authorization_state", state)
        return PlainTextResponse(
            "Account linked. You can close this tab and go back to SASH.",
        )

    @app.get("/ring/stats")
    async def ring_stats() -> dict[str, Any]:
        """What the front door has seen. Deliveries, rejections, duplicates."""
        return {
            "webhook": dict(receiver.stats),
            "ingest": processor.stats.as_dict(),
            "receipts": store.receipt_count(),
            "queue_depth": pipeline.depth(),
        }

    # --- the product -----------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return household.status()

    @app.get("/api/model")
    async def api_model() -> dict[str, Any]:
        return household.model_summary()

    @app.get("/api/timeline")
    async def api_timeline(hours: float = Query(24.0, ge=1.0, le=168.0)) -> dict[str, Any]:
        return household.timeline(hours)

    @app.get("/api/activity")
    async def api_activity() -> dict[str, Any]:
        """The street's weekly habit, which is what the planner forecasts from.

        Separate from the timeline because it is a different object: the
        timeline is what happened, this is what usually happens.
        """
        return household.street_profile()

    @app.get("/api/recommendation")
    async def api_recommendation(explore: bool = True) -> dict[str, Any]:
        rec = household.recommendation(explore=explore)
        provider, model_id, region = narrative.provider_from_env()
        rec["narrative"] = narrative.narrate(
            rec, provider=provider, model_id=model_id, region=region
        )
        return rec

    @app.post("/api/plan")
    async def api_offer_plan() -> dict[str, Any]:
        """Publish the current best plan into the ledger so it can be scored.

        A recommendation that is never written down cannot be wrong later, which
        is the whole reason this route exists.
        """
        row = household.offer()
        if row is None:
            raise HTTPException(409, "no plan available to publish yet")
        return row

    @app.post("/api/settle")
    async def api_settle() -> dict[str, Any]:
        return {"settled": household.settle_plans()}

    @app.get("/api/ledger")
    async def api_ledger() -> dict[str, Any]:
        household.settle_plans()
        return household.ledger()

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return household.health()

    @app.get("/api/devices")
    async def api_devices() -> dict[str, Any]:
        return {
            "devices": [
                {
                    "device_id": d.device_id,
                    "ring_name": d.ring_name,
                    "family": d.family,
                    "app_role": d.app_role,
                    "label": d.label,
                    "orientation": d.orientation,
                    "online": d.online,
                }
                for d in store.devices()
            ],
            "linked_to": (
                "Ring simulator" if config.ring.is_simulated else config.ring.api_base_url
            ),
            "synthetic_history": bool(store.get_meta("seeded_synthetic", False)),
        }

    @app.post("/api/devices/{device_id}/role")
    async def api_set_role(device_id: str, request: Request) -> dict[str, Any]:
        """Map a Ring device onto what it means in this home.

        Ring does not know that a contact sensor is on the courtyard window; its
        own documentation tells integrators to keep that meaning application
        side. This is where a household says it.
        """
        body = await request.json()
        if store.device(device_id) is None:
            raise HTTPException(404, "no such device")
        store.set_role(
            device_id,
            body.get("app_role", "unused"),
            body.get("label"),
            body.get("orientation"),
        )
        household.invalidate()
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "uptime_s": round(time.time() - app.state.started_at, 1),
            "queue_depth": pipeline.depth(),
            "simulated": config.ring.is_simulated,
            "demo_controls": on_demo_tick is not None,
            "demo_follow": on_demo_follow is not None,
            "now": household.now(),
        }

    # --- demo control ----------------------------------------------------

    if on_demo_tick is not None:

        @app.post("/api/demo/advance")
        async def api_demo_advance(minutes: float = Query(30.0, ge=1.0, le=720.0)) -> dict:
            """Run the simulated household forward and deliver what Ring would.

            Present only when SASH was started against the bundled simulator.
            Every event it produces goes through ``/ring/webhook`` with a real
            signature, so this button exercises the shipping path.
            """
            return await on_demo_tick(minutes)  # type: ignore[misc]

        @app.post("/api/demo/follow")
        async def api_demo_follow() -> dict:
            """Have the simulated household act on the plan SASH last published.

            The point of the button is that this is the only way a plan gets
            scored. SASH does not open windows, and a plan nobody acts on is
            recorded as exactly that.
            """
            if on_demo_follow is None:
                raise HTTPException(404, "not available")
            offered = [p for p in store.plans() if p["status"] == "offered"]
            if not offered:
                raise HTTPException(409, "publish a plan first")
            result = await on_demo_follow(offered[0])  # type: ignore[misc]
            household.invalidate()
            return result

    # --- the UI ----------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/app.css")
    async def css() -> FileResponse:
        return FileResponse(WEB_DIR / "app.css", media_type="text/css")

    @app.get("/app.js")
    async def js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")

    @app.get("/demo.js")
    async def demo_js() -> FileResponse:
        """The guided walkthrough at ``/?demo=1``.

        It clicks the product's own buttons and reads the product's own numbers,
        which is why the recording in ``demo_video/`` is a recording of SASH
        rather than an animation of it.
        """
        return FileResponse(WEB_DIR / "demo.js", media_type="application/javascript")

    return app
