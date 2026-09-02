"""The AWS deployment of SASH.

The shape is the local process, split along the seam that already exists.
Locally, ``ring/webhook.py`` verifies a delivery and hands it to
``ingest.Pipeline``; ``Pipeline`` is an asyncio queue with a ``put_nowait``.
On AWS the same verification runs in a Lambda behind API Gateway and the same
``put_nowait`` writes to SQS, and ``ingest.EventProcessor`` runs unchanged on the
other side of it.

So there are four handlers and one storage adapter here, and no second copy of
any rule. The signature check, the ``request_id`` idempotency, the inverted
contact vocabulary and the refusal to store a repeated ``reported_at`` are all
in the modules the local product uses.

Nothing in this package is imported unless it is being deployed, and ``boto3``
is a soft dependency (``pip install -e ".[aws]"``).
"""
