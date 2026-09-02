# SASH on AWS

Entry for the **AWS Builder** mini challenge.

**Stated plainly first: this stack has not been deployed.** The AWS credentials
available on the machine SASH was built on are invalid
(`aws sts get-caller-identity` returns `InvalidClientTokenId`), so what follows is
a SAM template, four Lambda handlers, a DynamoDB storage adapter and a Bedrock
narration adapter, all of them exercised by tests against doubles, and none of
them exercised against AWS itself. `docs/BENCHMARK.md` and the rest of the
product make no claims that depend on any of it.

---

## What is here

| Piece | File | Service |
|---|---|---|
| The stack | `infra/template.yaml` | API Gateway, Lambda, SQS, DynamoDB, EventBridge Scheduler, Secrets Manager, KMS, CloudWatch, SNS |
| Webhook handler | `src/sash/aws/webhook_handler.py` | Lambda behind API Gateway, writes to SQS |
| Processor handler | `src/sash/aws/processor_handler.py` | Lambda consuming SQS, writes DynamoDB |
| Poller handler | `src/sash/aws/poller_handler.py` | Lambda on an EventBridge schedule |
| API handler | `src/sash/aws/api_handler.py` | The FastAPI app behind Mangum |
| Storage adapter | `src/sash/aws/dynamo_store.py` | DynamoDB, single table |
| Narration adapter | `src/sash/narrative.py` | Amazon Bedrock |
| Tests | `tests/test_aws.py` | 18 tests against table and queue doubles |

---

## Why this shape

Ring expects a 200 within five seconds and retries otherwise. So the deployment
has the same seam the local process already has: a thin front that verifies,
deduplicates and acknowledges, and everything slower behind a queue.

```
Ring ──▶ API Gateway ──▶ webhook Lambda ──▶ SQS ──▶ processor Lambda ──▶ DynamoDB
                              │                                            ▲
                     verify HMAC over raw bytes                            │
                     conditional put on request_id           EventBridge ──┘
                     enqueue, return 200                     every 5 min:
                                                             poller Lambda
                                                             GET /devices/{id}/status

                         API Gateway ──▶ api Lambda (FastAPI/Mangum) ──▶ DynamoDB
                                              │
                                              └──▶ Bedrock (wording only)
```

`sash.ingest.Pipeline` is already SQS-shaped locally: it exposes `put_nowait`
and a consumer that calls `EventProcessor.apply`. On AWS the `put_nowait` sends
to SQS and the consumer is a Lambda. **That is the only adapter that differs.**
The signature check, the idempotency key, the inverted contact vocabulary and
the refusal to store a repeated `reported_at` are the same modules the local
product runs, so there is no second implementation of any rule that could drift.

---

## The two conditional writes

Everything the SQLite store enforces with a UNIQUE constraint becomes a
conditional write, because a read-then-write lets two concurrent Lambdas both
decide they were first.

**`RECEIPT#<request_id>`**, written with `attribute_not_exists(pk)`. A Ring retry
loses the race, is reported as a duplicate, and is acknowledged with a 200,
because from Ring's side a retry that is acknowledged is a retry that stops. The
receipt carries a seven-day TTL, which is all a retry window needs.

**`AQ#<reported_at>`**, written the same way. `GET /v1/devices/{id}/status`
returns the *last reported* reading, so a five-minute schedule against a
fifteen-minute device produces two repeats for every measurement. Storing those
as measurements would flatten every decay in the record. The refusal is the
common path, not the edge case, and `AirQualityNewSamples` falling to zero for an
hour is a CloudWatch alarm rather than a silent stall.

Timestamps in sort keys are zero-padded to fifteen characters so lexicographic
order is time order, which makes an interval scan one range query.

---

## Bedrock, and the line it does not cross

`narrative.py` has two implementations behind one interface. The default is a
deterministic phrase renderer: the same model state produces the same words, so
a screenshot can be checked against the numbers behind it. Setting
`BEDROCK_MODEL_ID` switches the wording to Bedrock.

The model is handed a **fact dictionary** and nothing else: the opening's name, a
start time, a duration, two forecast numbers, a percentage, and the reasons
airing is due. It is never sent readings, event times or contact history, and it
is never asked what to recommend. If it fails or is slow, the deterministic text
ships. **The numbers on screen are identical either way**, which is the point: an
adviser that cannot speak when the network is down is not an adviser, and a
number a language model produced is not a measurement.

---

## What deploying it would take

```bash
pip install -e ".[aws]"
sam build --template infra/template.yaml
sam deploy --guided --parameter-overrides Stage=prod BedrockModelId=<model-id>
```

Then put the Ring client id, client secret and HMAC signing key into the secret
the stack created, and register the two output URLs with Ring as the webhook
destination and the OAuth redirect.

The template is written and reviewed but has never been through `sam build`, so
expect it to need at least a `requirements.txt` beside `src/` and a Mangum
dependency added for the API function. Those are the first two things to hit.

---

## What the tests cover, and what they cannot

`tests/test_aws.py` drives the handlers against an in-memory table that
reproduces DynamoDB's conditional-put behaviour and a queue double. It covers a
signed delivery being queued, an unsigned one getting 401 and not being queued,
a base64 body verifying identically to a text one, a Ring retry reaching the
queue exactly once, a header in any case, the batch-item-failure protocol on one
malformed message, the role mapping surviving rediscovery, range queries over the
padded sort key, and the poller refusing a repeated reading.

What it cannot cover: whether those are actually DynamoDB's semantics, whether
API Gateway hands the body over the way the handler assumes, whether the IAM
policies are sufficient, and whether the Lambda package builds. A double written
by the same person as the client is not evidence about the vendor. Those are the
first things to check with working credentials, and until then this document says
so rather than implying a deployment.
