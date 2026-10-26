"""M21.1 — Telegram Bot API transport for signal alerts (send-only).

Credentials are constructor-injected (never read from the environment here);
the HTTP call is injectable so tests never touch the network. The token is
part of the request URL, so every error string is scrubbed of it before it
can reach a log, the outbox or an exception message.

Result classification drives the outbox's retry policy:
  * 2xx with ``ok: true``      -> sent
  * 429                        -> retryable, honouring ``retry_after``
  * 5xx / network / timeouts   -> retryable (exponential backoff upstream)
  * any other 4xx              -> permanent (bad token, chat not found, ...)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

HttpPost = Callable[[str, Dict[str, str], float], Tuple[int, str]]


@dataclass(frozen=True)
class SendResult:
    ok: bool
    retryable: bool = False
    status: Optional[int] = None
    error: Optional[str] = None
    retry_after: Optional[float] = None
    message_id: Optional[str] = None


def urllib_post(url: str, data: Dict[str, str], timeout: float) -> Tuple[int, str]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (fixed https host)
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


class TelegramTransport:
    def __init__(self, *, token: str, chat_id: str, http_post: HttpPost = urllib_post,
                 timeout: float = 10.0, disable_notification: bool = False):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("telegram token required")
        if not isinstance(chat_id, (str, int)) or not str(chat_id).strip():
            raise ValueError("telegram chat_id required")
        self._token = token.strip()
        self._chat_id = str(chat_id).strip()
        self._post = http_post
        self._timeout = float(timeout)
        self._silent = bool(disable_notification)

    def _scrub(self, text: Any) -> str:
        s = str(text)
        return s.replace(self._token, "<redacted>") if self._token else s

    def __repr__(self) -> str:                         # never expose the token
        return "TelegramTransport(chat_id=%r, token=<redacted>)" % self._chat_id

    def send(self, text: str) -> SendResult:
        data = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}
        if self._silent:
            data["disable_notification"] = "true"
        try:
            status, body = self._post(API_URL.format(token=self._token), data, self._timeout)
        except Exception as exc:                        # network, DNS, timeout, TLS ...
            return SendResult(ok=False, retryable=True,
                              error=self._scrub("network:%s:%s" % (type(exc).__name__, exc))[:300])
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        desc = self._scrub(payload.get("description") or "")[:200]
        if 200 <= status < 300 and payload.get("ok") is True:
            mid = (payload.get("result") or {}).get("message_id")
            return SendResult(ok=True, status=status, message_id=str(mid) if mid is not None else None)
        if status == 429:
            ra = (payload.get("parameters") or {}).get("retry_after")
            try:
                ra = max(1.0, float(ra))
            except (TypeError, ValueError):
                ra = None
            return SendResult(ok=False, retryable=True, status=status, retry_after=ra,
                              error="rate_limited:%s" % desc)
        if status >= 500 or (200 <= status < 300):      # 2xx without ok:true -> treat as transient
            return SendResult(ok=False, retryable=True, status=status,
                              error="server:%s:%s" % (status, desc))
        return SendResult(ok=False, retryable=False, status=status,
                          error="rejected:%s:%s" % (status, desc))
