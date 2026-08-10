"""Phone push notifications via ntfy.

ntfy needs no account: pick an unguessable topic name, subscribe to it in the
ntfy app, and anything POSTed to that topic lands on your phone. The topic name
is the only secret, which is why the README tells you to make it random --
'onepiece' would be readable (and writable) by strangers.
"""

from __future__ import annotations

import requests


class Notifier:
    def __init__(self, server: str, topic: str, *, enabled: bool = True):
        self._server = server.rstrip("/")
        self._topic = topic
        self.enabled = bool(enabled and topic)
        self._session = requests.Session()

    def send(
        self,
        *,
        title: str,
        message: str,
        priority: str = "default",
        tags: list[str] | None = None,
        click_url: str | None = None,
    ) -> bool:
        """Push one notification. Returns False on failure without raising.

        A dead notifier must never abort a price run -- the data is still worth
        collecting even if the phone push fails.
        """
        if not self.enabled:
            print(f"[notify:disabled] {title} -- {message}")
            return False

        headers = {
            "Title": _header_safe(title),
            "Priority": priority,
            "Markdown": "no",
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        if click_url:
            headers["Click"] = click_url

        try:
            resp = self._session.post(
                f"{self._server}/{self._topic}",
                data=message.encode("utf-8"),
                headers=headers,
                timeout=20,
            )
            if resp.status_code >= 300:
                print(f"[notify:error] ntfy returned {resp.status_code}: {resp.text[:200]}")
                return False
            return True
        except requests.RequestException as exc:
            print(f"[notify:error] {exc}")
            return False


def _header_safe(value: str) -> str:
    """HTTP headers are latin-1 and single-line; card names are neither."""
    collapsed = " ".join(value.split())
    return collapsed.encode("ascii", "replace").decode("ascii")[:200]
