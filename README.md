# custfind

Official Python client for the [Custfind API](https://custfind.com/developers): start lead searches, read scored leads, write and send emails, move deals and receive webhooks. Standard library only; Python 3.9+.

```bash
pip install custfind
```

```python
from custfind import Custfind

cf = Custfind()  # reads CUSTFIND_API_KEY

search = cf.searches.create(
    sector="Dental clinic",
    location="Kadıköy, Istanbul",
    limit=25,                        # caps the credits this search reserves
    external_customer_id="acme-42",  # optional: bill your own customer
)
cf.searches.wait_for_completion(search["job_id"])
for lead in cf.searches.all_leads(search["job_id"]):
    print(lead["name"], lead.get("email"), lead.get("score"))
```

## Safe retries

Every write is sent with an `Idempotency-Key` that stays the same across retries, and network errors, `429` (respecting `Retry-After`), `5xx` and `409 idempotency_in_progress` are retried up to `max_retries` (3). Any other `409` is final and raised at once. Every write method takes `idempotency_key=`. A timed-out search is never started twice. Pass your own key to make retries across restarts safe too:

```python
cf.searches.create(idempotency_key=f"search-{order_id}", sector="Dental clinic", location="Izmir", limit=50)
```

## Errors

Failed calls raise `CustfindError` with `.status` and the API's `.code` (`api_requires_paid_plan`, `customer_limit_reached`, `insufficient_scope`, …).

## Webhooks

```python
from flask import Flask, request, abort
from custfind import verify_webhook, WebhookVerificationError

@app.post("/webhooks/custfind")
def custfind_webhook():
    try:
        event = verify_webhook(request.get_data(), request.headers.get("Custfind-Signature"), SECRET)
    except WebhookVerificationError:
        abort(400)
    if event["type"] == "search.completed":
        ...
    return "", 200
```

## Resources

`searches`, `leads`, `outreach`, `pipelines`, `deals`, `webhooks`, `customers`, `sender_accounts`, plus `me()`, `balance()` and `usage()`. See the [API reference](https://custfind.com/developers).

## Contributing

This repository is a read-only mirror of `sdks/python` in Custfind's main repository, updated on every release. Issues are welcome here; pull requests are read and ported by hand, then closed, since the next release overwrites this repository.

## Developing

```bash
python -m unittest discover -s tests -t .
```

## Changelog

- **1.1.0** — Only `409 idempotency_in_progress` is retried (a final 409 used to be retried until it gave up). Read timeouts and dropped connections are retried on Python 3.9 too. Every write takes `idempotency_key=`. `searches.leads`/`all_leads` take every lead filter; new `leads.all`. `outreach.send` takes an aware `datetime`; `outreach.rewrite` needs a tone or an instruction. Plain-text error bodies are kept in the message.
