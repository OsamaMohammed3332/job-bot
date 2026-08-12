"""Thin Telegram Bot API client + message formatting."""

from __future__ import annotations

import html
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, log=print):
        self.token = token
        self.log = log
        self.sess = requests.Session()

    def _call(self, method: str, **params):
        url = API.format(token=self.token, method=method)
        try:
            r = self.sess.post(url, json=params, timeout=30)
        except requests.RequestException as e:
            self.log(f"  ! telegram network error on {method}: {e}")
            return None

        data = r.json() if r.content else {}
        if not data.get("ok"):
            # 429 -> Telegram tells us exactly how long to wait
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                self.log(f"  ! telegram rate limit, sleeping {retry}s")
                time.sleep(retry + 1)
                return self._call(method, **params)
            self.log(f"  ! telegram {method} failed: {data.get('description')}")
            return None
        return data.get("result")

    def send(self, chat_id: str | int, text: str, *, preview: bool = False):
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=not preview,
        )

    def get_updates(self, offset: int | None = None, timeout: int = 0):
        return self._call("getUpdates", offset=offset, timeout=timeout,
                          allowed_updates=["message"]) or []

    def me(self):
        return self._call("getMe")


def format_job(job) -> str:
    """Render one job in the same shape as the channel you showed."""
    e = html.escape
    lines = [
        f"<b>{e(job.title)}</b>",
        f"🏢 {e(job.company)}",
        f"📍 {e(job.location)}",
        f"🕒 {e(job.age_text())}",
        f"📌 via {e(job.source)}",
        "",
        f'➡️ <a href="{e(job.url, quote=True)}">Apply Here</a>',
    ]
    return "\n".join(lines)


HELP = """<b>Job Bot</b> — LinkedIn + remote job alerts

<b>Commands</b>
/start — subscribe to DM alerts
/stop — unsubscribe
/keywords python, flutter, react — only DM me jobs matching these
/keywords clear — remove keyword filter
/locations egypt, remote — only DM me jobs in these places
/locations clear — remove location filter
/search flutter egypt — search right now
/status — show your current filters

Leave filters empty and you get everything the channel gets."""
