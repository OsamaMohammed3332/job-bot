"""Thin Telegram Bot API client + message formatting."""

from __future__ import annotations

import html
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"

LEVEL_LABELS = {
    "junior": "Junior",
    "mid": "Mid-level",
    "senior": "Senior",
}


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

    def send(self, chat_id: str | int, text: str, *, preview: bool = False,
             keyboard: list | None = None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self._call("sendMessage", **params)

    def answer_callback(self, callback_id: str, text: str = ""):
        return self._call("answerCallbackQuery",
                          callback_query_id=callback_id, text=text)

    def edit_markup(self, chat_id, message_id, keyboard: list):
        """Redraw the buttons in place so ticks appear as you tap."""
        return self._call(
            "editMessageReplyMarkup",
            chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": keyboard},
        )

    def get_updates(self, offset: int | None = None, timeout: int = 0):
        return self._call(
            "getUpdates", offset=offset, timeout=timeout,
            allowed_updates=["message", "callback_query"],
        ) or []

    def me(self):
        return self._call("getMe")


# --------------------------------------------------------------------------
#  Keyboards
# --------------------------------------------------------------------------

def _tick(text: str, on: bool) -> str:
    return f"✅ {text}" if on else text


def level_keyboard(selected: list | None = None) -> list:
    """Multi-select. An empty selection means no restriction, so the
    'All levels' row is the one ticked when nothing else is."""
    s = set(selected or [])
    return [
        [{"text": _tick("Junior", "junior" in s), "callback_data": "level:junior"},
         {"text": _tick("Mid-level", "mid" in s), "callback_data": "level:mid"}],
        [{"text": _tick("Senior", "senior" in s), "callback_data": "level:senior"}],
        [{"text": _tick("All levels", not s), "callback_data": "level:all"}],
    ]


def track_keyboard(tracks: dict, selected: list | None = None) -> list:
    """One button per configured track, plus an 'everything' reset."""
    s = set(selected or [])
    rows, row = [], []
    for name, t in tracks.items():
        row.append({"text": _tick(t.get("label", name.title()), name in s),
                    "callback_data": f"track:{name}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": _tick("Everything", not s),
                  "callback_data": "track:all"}])
    return rows


# --------------------------------------------------------------------------
#  Message formatting
# --------------------------------------------------------------------------

def format_job(job, tracks: dict | None = None) -> str:
    e = html.escape
    label = (tracks or {}).get(job.track, {}).get("label", job.track.title())
    level = LEVEL_LABELS.get(job.level, job.level.title())

    lines = [
        f"<b>{e(job.title)}</b>",
        f"🏢 {e(job.company)}",
        f"📍 {e(job.location)}",
        f"🕒 {e(job.age_text())}",
    ]
    if label or level:
        lines.append(f"🎯 {e(label)} · {e(level)}")
    lines += [
        f"📌 via {e(job.source)}",
        "",
        f'➡️ <a href="{e(job.url, quote=True)}">Apply Here</a>',
    ]
    return "\n".join(lines)


WELCOME = """👋 <b>Job Bot</b>

I watch LinkedIn and remote job boards and message you the moment something matching shows up — usually within a few minutes of it going live.

Pick what you want and you're done:

/track — Flutter, Odoo, or everything
/level — Junior, Mid, Senior, or any mix

Both are multi-select: tap to add a ✅, tap again to remove it. Pick Mid <i>and</i> Senior if you want both.

Right now you're set to receive <b>everything</b>. Tap /track to narrow it down."""


HELP = """<b>Job Bot commands</b>

/track — Flutter, Odoo, or everything (multi-select)
/level — Junior, Mid, Senior, or any mix (multi-select)

Tap a button to add ✅, tap again to remove. Selecting none means no
restriction, so you will never end up with an empty feed by accident.
/status — show your current settings
/search flutter — search right now
/keywords python, laravel — extra title words you care about
/keywords clear — remove that filter
/locations egypt, remote — restrict by place
/stop — stop all alerts
/start — start again

Filters you don't set are left wide open."""
