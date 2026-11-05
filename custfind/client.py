import datetime
import http.client
import json
import os
import random
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Union
from urllib import error, parse, request

__version__ = "1.1.0"

DEFAULT_BASE_URL = "https://api.custfind.com"
FINAL_STATUSES = ("completed", "failed", "cancelled")


class CustfindError(Exception):
    """An error response from the Custfind API, or a request that never got one."""

    def __init__(self, status: int, code: str, message: Optional[str] = None, retry_after: Optional[int] = None):
        self.status = status  # 0 when there was no response
        self.code = code
        self.retry_after = retry_after
        super().__init__(message or (f"Custfind API error {status}: {code}" if status else f"Custfind request failed: {code}"))


def _q(value: str) -> str:
    return parse.quote(str(value), safe="")


class Custfind:
    """The Custfind API client.

    Every write carries an Idempotency-Key that stays the same across its
    retries, so a retried search never starts twice or reserves credits twice.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("CUSTFIND_API_KEY")
        if not self.api_key:
            raise ValueError("Custfind: pass api_key or set CUSTFIND_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.searches = Searches(self)
        self.leads = Leads(self)
        self.outreach = Outreach(self)
        self.pipelines = Pipelines(self)
        self.deals = Deals(self)
        self.webhooks = Webhooks(self)
        self.customers = Customers(self)
        self.sender_accounts = SenderAccounts(self)

    def me(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/me")

    def balance(self) -> Dict[str, Any]:
        return self.request("GET", "/v1/balance")

    def usage(self, **params: Any) -> Dict[str, Any]:
        """from, to, group_by ("customer" | "api_key" | "none"), external_customer_id."""
        return self.request("GET", "/v1/usage", query=params)

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        body: Any = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if clean:
                url += "?" + parse.urlencode(clean)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"custfind-python/{__version__}",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if method != "GET":
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())

        attempt = 0
        while True:
            req = request.Request(url, data=data, method=method, headers=headers)
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.status == 204 or not raw:
                        return None
                    return json.loads(raw)
            except error.HTTPError as exc:
                retry_after = _int(exc.headers.get("Retry-After"))
                try:
                    raw = exc.read() or b""
                except (OSError, http.client.HTTPException):
                    raw = b""
                finally:
                    exc.close()
                code, detail = f"http_{exc.code}", None
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        code = payload.get("error") or code
                        detail = payload.get("detail")
                except ValueError:
                    # A few routes answer in plain text ("invalid json body").
                    detail = raw.decode("utf-8", "replace").strip() or None
                # A 409 is retried only while an earlier attempt of the same
                # write is still running; any other 409 ("invalid job state")
                # is final, and a retry would only get it replayed.
                retryable = exc.code == 429 or exc.code >= 500 or (exc.code == 409 and code == "idempotency_in_progress")
                if retryable and attempt < self.max_retries:
                    time.sleep(retry_after if retry_after is not None else _backoff(attempt))
                    attempt += 1
                    continue
                message = f"Custfind API error {exc.code}: {code} ({detail})" if detail else None
                raise CustfindError(exc.code, code, message, retry_after=retry_after) from None
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts (socket.timeout is not a TimeoutError on
                # 3.9), dropped connections and truncated responses.
                if attempt < self.max_retries:
                    time.sleep(_backoff(attempt))
                    attempt += 1
                    continue
                raise CustfindError(0, str(getattr(exc, "reason", exc))) from None


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * 2**attempt) + random.random() * 0.25


def _paginate(fetch: Callable[[int], Dict[str, Any]], page_size: int) -> Iterator[Dict[str, Any]]:
    page = 1
    while True:
        res = fetch(page)
        items = res.get("items") or []
        yield from items
        if not items or page * page_size >= res.get("total", 0):
            return
        page += 1


class _Resource:
    def __init__(self, client: Custfind):
        self._c = client


class Searches(_Resource):
    def create(self, idempotency_key: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        """sector, location, limit, external_customer_id, search_mode, country_code, … Set limit: it caps the credits reserved."""
        return self._c.request("POST", "/v1/searches", body=params, idempotency_key=idempotency_key)

    def get(self, search_id: str) -> Dict[str, Any]:
        return self._c.request("GET", f"/v1/searches/{_q(search_id)}")

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._c.request("GET", "/v1/searches", query={"limit": limit})

    def cancel(self, search_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request("POST", f"/v1/searches/{_q(search_id)}/cancel", idempotency_key=idempotency_key)

    def leads(self, search_id: str, page: int = 1, page_size: int = 25, **filters: Any) -> Dict[str, Any]:
        """One page of a search's leads; takes the same filters as leads.list."""
        filters.pop("job_id", None)
        return self._c.request("GET", f"/v1/searches/{_q(search_id)}/leads", query={"page": page, "page_size": page_size, **filters})

    def all_leads(self, search_id: str, page_size: int = 100, **filters: Any) -> Iterator[Dict[str, Any]]:
        """Every lead of a search, page by page. Iterate a finished search: while one runs, new leads can shift the pages."""
        return _paginate(lambda page: self.leads(search_id, page=page, page_size=page_size, **filters), page_size)

    def wait_for_completion(self, search_id: str, interval: float = 3.0, timeout: float = 1800.0) -> Dict[str, Any]:
        """Poll until the search completes, fails or is cancelled."""
        deadline = time.monotonic() + timeout
        while True:
            search = self.get(search_id)
            if search.get("status") in FINAL_STATUSES:
                return search
            if time.monotonic() + interval > deadline:
                raise CustfindError(0, "wait_timeout", f"search {search_id} still {search.get('status')}")
            time.sleep(interval)


class Leads(_Resource):
    def list(self, **params: Any) -> Dict[str, Any]:
        """page, page_size (max 100), q, job_id, category, contact (email|phone|both|website|none),
        min_score, max_score, min_rating, max_rating, region, language, source,
        outreach_status (none|pending|sent|opened|failed), unsubscribed (yes|no), stage_id, tag_id,
        sort (score_desc, rating_desc, created_asc, name, …; newest first when omitted)."""
        return self._c.request("GET", "/v1/leads", query=params)

    def all(self, page_size: int = 100, **filters: Any) -> Iterator[Dict[str, Any]]:
        """Every lead matching the filters, page by page."""
        filters.pop("page", None)
        return _paginate(lambda page: self.list(page=page, page_size=page_size, **filters), page_size)

    def get(self, lead_id: str) -> Dict[str, Any]:
        """The lead with its enrichment, emails and calls: {"lead", "enrichment", "outreach", "calls"}."""
        return self._c.request("GET", f"/v1/leads/{_q(lead_id)}")

    def outreach(self, lead_id: str) -> List[Dict[str, Any]]:
        return self._c.request("GET", f"/v1/leads/{_q(lead_id)}/outreach")

    def write_draft(self, lead_id: str, regenerate: bool = False, idempotency_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Write the lead's draft if missing; regenerate=True rewrites it (one AI edit)."""
        return self._c.request("POST", f"/v1/leads/{_q(lead_id)}/outreach", body={"regenerate": regenerate}, idempotency_key=idempotency_key)

    def crm(self, lead_id: str) -> Dict[str, Any]:
        """The lead's deal, notes, tasks, tags, emails and meetings."""
        return self._c.request("GET", f"/v1/leads/{_q(lead_id)}/crm")

    def add_note(self, lead_id: str, body: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request("POST", f"/v1/leads/{_q(lead_id)}/notes", body={"body": body}, idempotency_key=idempotency_key)


class Outreach(_Resource):
    def list(self, **params: Any) -> Dict[str, Any]:
        """page, page_size, job_id, status (draft|scheduled|sent|failed), channel, q,
        sort (created_at_asc|sent_at_desc|sent_at_asc|lead_name_asc|lead_name_desc|engagement_score_desc).
        The result has "items", "total", "page", "page_size" and "stats"."""
        return self._c.request("GET", "/v1/outreach", query=params)

    def get(self, message_id: str) -> Dict[str, Any]:
        return self._c.request("GET", f"/v1/outreach/{_q(message_id)}")

    def update(self, message_id: str, message: str, email_subject: Optional[str] = None, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"message": message}
        if email_subject is not None:
            body["email_subject"] = email_subject
        return self._c.request("PATCH", f"/v1/outreach/{_q(message_id)}", body=body, idempotency_key=idempotency_key)

    def rewrite(
        self, message_id: str, tone: Optional[str] = None, instruction: Optional[str] = None, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Rewrite with AI by tone (professional|short|casual) or instruction; one AI edit, refunded on failure."""
        if not tone and not instruction:
            raise ValueError("Custfind: rewrite needs a tone or an instruction")
        body = {k: v for k, v in {"tone": tone, "instruction": instruction}.items() if v}
        return self._c.request("POST", f"/v1/outreach/{_q(message_id)}/rewrite", body=body, idempotency_key=idempotency_key)

    def send(
        self, message_id: str, scheduled_at: Union[str, datetime.datetime, None] = None, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send now, or at scheduled_at (an aware datetime or an RFC 3339 string)."""
        if isinstance(scheduled_at, datetime.datetime):
            if scheduled_at.tzinfo is None:
                raise ValueError("Custfind: scheduled_at needs a timezone")
            scheduled_at = scheduled_at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"scheduled_at": scheduled_at} if scheduled_at else {}
        return self._c.request("POST", f"/v1/outreach/{_q(message_id)}/send", body=body, idempotency_key=idempotency_key)


class Pipelines(_Resource):
    def list(self) -> List[Dict[str, Any]]:
        return self._c.request("GET", "/v1/pipelines")


class Deals(_Resource):
    def create(self, lead_id: str, idempotency_key: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        """stage_id, title, value_cents, currency, assigned_user_id. Without stage_id the deal starts in the default pipeline's first stage."""
        return self._c.request("POST", "/v1/deals", body={"lead_id": lead_id, **params}, idempotency_key=idempotency_key)

    def update(self, deal_id: str, idempotency_key: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        """stage_id, pipeline_id, title, value_cents, currency, assigned_user_id."""
        return self._c.request("PATCH", f"/v1/deals/{_q(deal_id)}", body=params, idempotency_key=idempotency_key)


class Webhooks(_Resource):
    def create(self, url: str, events: List[str], description: str = "", idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Register an endpoint; the returned "secret" is shown only now."""
        body = {"url": url, "events": events, "description": description}
        return self._c.request("POST", "/v1/webhooks", body=body, idempotency_key=idempotency_key)

    def list(self) -> List[Dict[str, Any]]:
        return self._c.request("GET", "/v1/webhooks")

    def update(self, endpoint_id: str, idempotency_key: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        """enabled, events, url, description."""
        return self._c.request("PATCH", f"/v1/webhooks/{_q(endpoint_id)}", body=params, idempotency_key=idempotency_key)

    def delete(self, endpoint_id: str, idempotency_key: Optional[str] = None) -> None:
        self._c.request("DELETE", f"/v1/webhooks/{_q(endpoint_id)}", idempotency_key=idempotency_key)

    def send_test(self, endpoint_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request("POST", f"/v1/webhooks/{_q(endpoint_id)}/test", idempotency_key=idempotency_key)

    def deliveries(self, endpoint_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._c.request("GET", f"/v1/webhooks/{_q(endpoint_id)}/deliveries", query={"limit": limit})


class Customers(_Resource):
    def list(self) -> List[Dict[str, Any]]:
        return self._c.request("GET", "/v1/customers")

    def get(self, customer_id: str) -> Dict[str, Any]:
        return self._c.request("GET", f"/v1/customers/{_q(customer_id)}")

    def set_limit(self, customer_id: str, monthly_credits: int, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        body = {"monthly_credits": monthly_credits}
        return self._c.request("PUT", f"/v1/customers/{_q(customer_id)}/limit", body=body, idempotency_key=idempotency_key)

    def remove_limit(self, customer_id: str, idempotency_key: Optional[str] = None) -> None:
        self._c.request("DELETE", f"/v1/customers/{_q(customer_id)}/limit", idempotency_key=idempotency_key)


class SenderAccounts(_Resource):
    def list(self, external_customer_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._c.request("GET", "/v1/sender-accounts", query={"external_customer_id": external_customer_id})

    def create(self, idempotency_key: Optional[str] = None, **params: Any) -> Dict[str, Any]:
        """name, email, smtp_host, smtp_port, smtp_user, smtp_pass, external_customer_id (optional)."""
        return self._c.request("POST", "/v1/sender-accounts", body=params, idempotency_key=idempotency_key)

    def delete(self, account_id: str, idempotency_key: Optional[str] = None) -> None:
        self._c.request("DELETE", f"/v1/sender-accounts/{_q(account_id)}", idempotency_key=idempotency_key)
