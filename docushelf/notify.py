"""Optional push notifications (ntfy topic URL or any webhook accepting POST)."""

from __future__ import annotations

import logging
import threading
from email.header import Header

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, url: str | None):
        self.url = url

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def send(self, title: str, message: str, *, tags: str = "warning", click: str | None = None) -> None:
        """Fire and forget; failures are only logged.

        ``click`` is a link the notification opens when tapped (ntfy's
        ``Click`` header; other webhooks ignore it).
        """
        if not self.url:
            return

        headers = _headers(title, tags, click)

        def _post() -> None:
            try:
                httpx.post(
                    self.url,  # type: ignore[arg-type]
                    content=message.encode("utf-8"),
                    headers=headers,
                    timeout=10,
                )
            except httpx.HTTPError:
                log.warning("notification failed", exc_info=True)

        threading.Thread(target=_post, name="notify", daemon=True).start()

    def send_now(self, title: str, message: str, *, tags: str = "white_check_mark", timeout: float = 10) -> str | None:
        """Send right away and wait for the answer (the "Test" button of the settings).

        Returns None on success, else a German reason; never raises.
        """
        if not self.url:
            return "Es ist keine Benachrichtigungs-Adresse eingerichtet."
        try:
            response = httpx.post(
                self.url,
                content=message.encode("utf-8"),
                headers=_headers(title, tags, None),
                timeout=timeout,
            )
        except httpx.TimeoutException:
            return f"Keine Antwort nach {int(timeout)} s."
        except httpx.HTTPError as exc:
            return f"Keine Verbindung ({type(exc).__name__})."
        if response.status_code >= 400:
            return f"Der Dienst antwortet mit HTTP {response.status_code}."
        return None


def _headers(title: str, tags: str, click: str | None) -> dict[str, str]:
    # HTTP headers are latin-1; non-ASCII titles go out as RFC 2047 encoded words.
    header_title = title if title.isascii() else Header(title, "utf-8").encode()
    headers = {"Title": header_title, "Tags": tags}
    if click and click.isascii():
        headers["Click"] = click
    return headers
