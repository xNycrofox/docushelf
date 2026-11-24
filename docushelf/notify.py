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

        # HTTP headers are latin-1; non-ASCII titles go out as RFC 2047 encoded words.
        header_title = title if title.isascii() else Header(title, "utf-8").encode()
        headers = {"Title": header_title, "Tags": tags}
        if click and click.isascii():
            headers["Click"] = click

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
