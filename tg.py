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
             markup: dict | None = None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if markup:
            params["reply_markup"] = markup
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

DONE_LABEL = "✔️ Done"


def _tick(text: str, on: bool) -> str:
    return f"✅ {text}" if on else text


def strip_tick(text: str) -> str:
    """Buttons send back their own label, ticks included. Strip it."""
    return text.replace("✅", "").strip()


def _reply_kb(rows: list[list[str]]) -> dict:
    """A REPLY keyboard, not an inline one.

    Inline buttons are wrong for this bot: Telegram spins a loader on them
    until the bot answers the callback, and swallows further taps meanwhile.
    Since the bot only wakes every couple of minutes the query has usually
    expired by then, so the button jams. Reply-keyboard buttons just send a
    normal message - nothing to answer, nothing to expire.
    """
    return {
        "keyboard": [[{"text": c} for c in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }


REMOVE_KB = {"remove_keyboard": True}


def level_markup(selected: list | None = None) -> dict:
    s = set(selected or [])
    return _reply_kb([
        [_tick("Junior", "junior" in s), _tick("Mid-level", "mid" in s)],
        [_tick("Senior", "senior" in s), _tick("All levels", not s)],
        [DONE_LABEL],
    ])


def track_markup(tracks: dict, selected: list | None = None) -> dict:
    s = set(selected or [])
    labels = [_tick(t.get("label", n.title()), n in s) for n, t in tracks.items()]
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append([_tick("Everything", not s)])
    rows.append([DONE_LABEL])
    return _reply_kb(rows)


def button_actions(tracks: dict) -> dict:
    """Map a tapped button label to (field, value)."""
    m = {
        "Junior": ("levels", "junior"),
        "Mid-level": ("levels", "mid"),
        "Senior": ("levels", "senior"),
        "All levels": ("levels", "all"),
        "Everything": ("tracks", "all"),
    }
    for name, t in tracks.items():
        m[t.get("label", name.title())] = ("tracks", name)
    return m

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

Buttons appear above your keyboard. Tap every option you want - pick Mid <i>and</i> Senior if that is your range. Tap <b>All levels</b> / <b>Everything</b> to start over, then <b>✔️ Done</b>.

Replies take a minute or two to arrive; that is normal.

Right now you're set to receive <b>everything</b>. Tap /track to narrow it down."""


HELP = """<b>Job Bot commands</b>

/track — Flutter, Odoo, or everything
/level — Junior, Mid, Senior, or any mix
/status — show your current settings
/search flutter — search right now
/keywords python, laravel — extra title words you care about
/keywords clear — remove that filter
/locations egypt, remote — restrict by place
/stop — stop all alerts
/start — start again

<b>Track and level are multi-select.</b> Tap every option you want, then tap <b>✔️ Done</b>. A ✅ marks what you have chosen. Tapping <b>All levels</b> or <b>Everything</b> clears your choices and puts you back to receiving all of them.

The bot checks for jobs every couple of minutes, so replies are not instant.

Selecting nothing means no restriction, so you will never end up with an empty feed by accident."""
