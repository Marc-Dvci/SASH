# SASH on AWS

Entry for the **AWS Builder** mini challenge. The stack is deployed in
`us-east-1` as `sash-dev`, seeded with the same 120-day household as
`sash demo`:

<https://i1jcuqoto7.execute-api.us-east-1.amazonaws.com/dev/>

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
| Table seeder | `infra/seed_table.py` | Builds the demo household through the receiver and the poller, and writes it to the table |
| Narration adapter | `src/sash/narrative.py` | Amazon Bedrock |
| Tests | `tests/test_aws.py` | 19 tests against table and queue doubles |

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
product runs, so there is one implementation of every rule.

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
common path, and `AirQualityNewSamples` falling to zero for an hour is a
CloudWatch alarm.

Timestamps in sort keys are zero-padded to fifteen characters so lexicographic
order is time order, which makes an interval scan one range query.

---

## Verified against the live stack

Run on 17 September 2026 with `tests/test_ring_contract.py`'s documented
payloads, signed with the key in the stack's secret:

| Request | Answer |
|---|---|
| A signed `contact_sensor_faulted` | `200 {"ok": true, "reason": "queued"}` |
| The same delivery again, same `meta.request_id` | `200 {"ok": true, "reason": "duplicate request_id, already processed"}` in 0.25 s |
| The same body with a forged `X-Signature` | `401 {"ok": false, "reason": "X-Signature does not match the signing key"}` |
| A signed `motion_detected` with `sub_type: vehicle` | queued, then read back from `GET /api/status` through SQS, the processor and DynamoDB |
| `GET /api/recommendation` | the demo's decision, word for word: *Open the Courtyard window at 06:45 for 57 minutes ... 388 ... 597 ... 35 per cent less ... 97 per cent of paired draws* |

The webhook Lambda answers in 2.3 s on a cold start including the Secrets
Manager read and in about 0.25 s warm, inside Ring's five-second budget. The
product routes answer in under 0.4 s warm: the DynamoDB store reuses a range
read for a minute inside a container, and a five-minute schedule keeps one API
container holding the fitted household view.

`infra/seed_table.py` fills the table: it builds the demo household through the
webhook receiver and the poller into a local SQLite file, then writes its
84,246 items through `DynamoStore` in 84 s. The deployed product's clock is the
tape's cursor, as in `sash demo`, so the decision on AWS is the decision on a
laptop.

---

## Bedrock, and the line it does not cross

`narrative.py` has two implementations behind one interface. The default is a
deterministic phrase renderer: the same model state produces the same words, so
a screenshot can be checked against the numbers behind it. Setting
`BedrockModelId` on the stack switches the wording of the decision card to
Bedrock through the Converse API.

The model is handed a **fact dictionary** and nothing else: the opening's name, a
start time, a duration, two forecast numbers, a percentage, and the reasons
airing is due. It is never sent readings, event times or contact history, and it
is never asked what to recommend. If it fails or is slow, the deterministic text
ships. **The numbers on screen are identical either way**: a number a language
model produced is not a measurement.

---

## Deploying it

```bash
pip install -e ".[aws]"
sam build --use-container --template infra/template.yaml
sam deploy --stack-name sash-dev --resolve-s3 --capabilities CAPABILITY_IAM \
    --parameter-overrides Stage=dev BedrockModelId=us.openai.gpt-5.6-luna \n    BedrockFoundationModel=openai.gpt-5.6-luna
```

Then write the Ring client id, client secret, HMAC signing key and access
token into the secret the stack created, and register the two output URLs with
Ring as the webhook destination and the OAuth redirect:

```bash
aws secretsmanager put-secret-value --secret-id <SecretArn> --secret-string \
  '{"client_id":"...","client_secret":"...","hmac_signing_key":"...","access_token":"...","refresh_token":""}'
python infra/seed_table.py --table sash-dev-household   # optional: the demo household
```

`--use-container` builds the `arm64` packages in the Lambda image, so numpy
arrives as the right wheel. The web app is served from the stage path
(`/dev/`), and every request the page makes is relative to its own directory,
so the same files serve locally at `/` and behind API Gateway.

---

## What the tests cover

`tests/test_aws.py` drives the handlers against an in-memory table that
reproduces DynamoDB's conditional-put behaviour and a queue double. It covers a
signed delivery being queued, an unsigned one getting 401 and not being queued,
a base64 body verifying identically to a text one, a Ring retry reaching the
queue exactly once, a header in any case, the batch-item-failure protocol on one
malformed message, the role mapping surviving rediscovery, range queries over the
padded sort key, reserved attribute names going through placeholders, the
deployed clock following a seeded tape, and the poller refusing a repeated
reading. The table above is
the same sequence run against the deployed stack.
