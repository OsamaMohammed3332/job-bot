#!/usr/bin/env python3
"""
Telegram job bot - multi-track, multi-subscriber.

The bot fetches a SUPERSET of jobs (every configured track, every seniority
level) and each subscriber filters it their own way via /track and /level.
That way one deployment serves any number of people with different tastes.

Modes
-----
  python bot.py once     Fetch, deliver, handle commands, save, exit.
  python bot.py serve    Same on a loop, with instant command replies.
  python bot.py test     Fetch and print. Sends nothing to Telegram.

Environment
-----------
  BOT_TOKEN    required - from @BotFather
  CHANNEL_ID   optional - @yourchannel or -100123...; omit for DM-only
  POLL_SECONDS optional - serve-mode interval, default 300
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import yaml

from sources import (
    Job,
    classify_level,
    fetch_arbeitnow,
    fetch_linkedin,
    fetch_remoteok,
    fetch_remotive,
)
from store import Store
from tg import (
    HELP,
    WELCOME,
    LEVEL_LABELS,
    Telegram,
    format_job,
    level_keyboard,
    track_keyboard,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")
STATE_PATH = os.path.join(HERE, "state.json")

LEVELS = ("junior", "mid", "senior")


def log(*a):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}]", *a, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
#  Filtering
# ---------------------------------------------------------------------------

def passes_global_filters(job: Job, filters: dict) -> bool:
    """Rules that apply to every track: geography, blocklists, junk titles."""
    title = job.title.lower()
    location = job.location.lower()

    blocked = [c.lower() for c in filters.get("blocked_companies") or []]
    if any(b in job.company.lower() for b in blocked):
        return False

    for loc in filters.get("blocked_locations") or []:
        if loc.lower() in location:
            return False

    # Geography gate. A job is only useful if it is either where you live or
    # genuinely remote. LinkedIn's guest endpoint ignores its own remote
    # filter and the cards carry no work-type field, so this is the backstop.
    home = [h.lower() for h in filters.get("home_locations") or []]
    markers = [m.lower() for m in filters.get("remote_markers") or []]
    if home or markers:
        if not (any(h in location for h in home)
                or any(m in location for m in markers)):
            return False

    bad = [w.lower() for w in filters.get("title_must_not_include") or []]
    if any(w in title for w in bad):
        return False

    return True


def matches_track(job: Job, track: dict) -> bool:
    """Does this job's title fit the given track?

    LinkedIn results get the broader list, because the search query already
    narrowed them down. API results get the strict list, because those feeds
    return all of software development.
    """
    key = ("title_must_include_linkedin"
           if job.source == "LinkedIn" else "title_must_include")
    words = [w.lower() for w in track.get(key) or []]
    if not words:
        return True
    title = job.title.lower()
    return any(w in title for w in words)


def passes_filters(job: Job, cfg: dict) -> bool:
    """Full check: global rules plus the job's own track rules."""
    if not passes_global_filters(job, cfg.get("filters") or {}):
        return False
    track = (cfg.get("tracks") or {}).get(job.track)
    if track is None:
        return False
    return matches_track(job, track)


# ---------------------------------------------------------------------------
#  Collect
# ---------------------------------------------------------------------------

def collect(cfg: dict) -> list[Job]:
    """Run every track's searches plus the shared APIs, then merge."""
    lookback = cfg.get("lookback_seconds", 21600)
    tracks = cfg.get("tracks") or {}
    jobs: list[Job] = []

    for track_name, track in tracks.items():
        for s in track.get("searches") or []:
            kw = s.get("keywords", "")
            found = fetch_linkedin(
                kw,
                s.get("location", ""),
                geo_id=s.get("geo_id"),
                lookback_seconds=lookback,
                exp=s.get("exp"),
                log=log,
            )
            for j in found:
                j.track = track_name
            log(f"  LinkedIn [{track_name}: {kw}] -> {len(found)}")
            jobs.extend(found)

    # Shared API sources. Each result is assigned to the first track whose
    # strict include list it matches; unmatched results are discarded.
    extra = cfg.get("extra_sources") or {}
    cutoff = time.time() - lookback
    for name, fn in (
        ("remotive", fetch_remotive),
        ("remoteok", fetch_remoteok),
        ("arbeitnow", fetch_arbeitnow),
    ):
        if not extra.get(name):
            continue
        fresh = [
            j for j in fn(log=log)
            if j.posted_at is None or j.posted_at.timestamp() >= cutoff
        ]
        kept = 0
        for j in fresh:
            for track_name, track in tracks.items():
                if matches_track(j, track):
                    j.track = track_name
                    jobs.append(j)
                    kept += 1
                    break
        log(f"  {name} -> {kept} matched (of {len(fresh)} recent)")

    # Dedupe by id, then by (title, company) to catch cross-postings.
    seen_ids, seen_pairs, unique = set(), set(), []
    for j in jobs:
        pair = (j.title.lower().strip(), j.company.lower().strip())
        if j.id in seen_ids or pair in seen_pairs:
            continue
        seen_ids.add(j.id)
        seen_pairs.add(pair)
        j.level = classify_level(j.title)
        unique.append(j)

    unique.sort(
        key=lambda j: j.posted_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return unique


# ---------------------------------------------------------------------------
#  Per-subscriber delivery rules
# ---------------------------------------------------------------------------

def matches_subscriber(job: Job, sub: dict) -> bool:
    """Empty list = no restriction, so a new subscriber gets everything."""
    tracks = sub.get("tracks") or []
    if tracks and job.track not in tracks:
        return False

    levels = sub.get("levels") or []
    if levels and job.level not in levels:
        return False

    hay = job.haystack()
    kws = sub.get("keywords") or []
    if kws and not any(k in hay for k in kws):
        return False

    locs = sub.get("locations") or []
    if locs and not any(l in job.location.lower() or l in hay for l in locs):
        return False

    return True


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------

def _parse_list(arg: str) -> list[str]:
    return [p.strip().lower() for p in arg.replace(",", " ").split() if p.strip()]


def _status_text(sub: dict, tracks: dict) -> str:
    if not sub:
        return "You're not subscribed. Send /start."
    chosen = sub.get("tracks") or []
    tl = ", ".join(tracks.get(t, {}).get("label", t) for t in chosen) or "everything"
    lv = ", ".join(LEVEL_LABELS.get(l, l) for l in (sub.get("levels") or [])) or "all levels"
    return (
        "<b>Your settings</b>\n"
        f"🎯 Track: {tl}\n"
        f"📊 Level: {lv}\n"
        f"🔑 Keywords: {', '.join(sub.get('keywords') or []) or 'any'}\n"
        f"📍 Locations: {', '.join(sub.get('locations') or []) or 'any'}"
    )


def _handle_callback(tg: Telegram, store: Store, cfg: dict, cq: dict) -> None:
    data = cq.get("data") or ""
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    tracks = cfg.get("tracks") or {}
    if not chat_id:
        return
    store.subscribe(chat_id)

    if data.startswith("level:"):
        val = data.split(":", 1)[1]
        levels = [] if val == "all" else [val]
        store.set_filter(chat_id, "levels", levels)
        label = "all levels" if not levels else LEVEL_LABELS.get(val, val)
        tg.answer_callback(cq["id"], f"Level: {label}")
        tg.send(chat_id, f"📊 Level set to <b>{label}</b>.")

    elif data.startswith("track:"):
        val = data.split(":", 1)[1]
        chosen = [] if val == "all" else [val]
        store.set_filter(chat_id, "tracks", chosen)
        label = ("everything" if not chosen
                 else tracks.get(val, {}).get("label", val))
        tg.answer_callback(cq["id"], f"Track: {label}")
        tg.send(chat_id, f"🎯 Track set to <b>{label}</b>.")
    else:
        tg.answer_callback(cq["id"])


def handle_commands(tg: Telegram, store: Store, cfg: dict) -> None:
    """Drain pending Telegram updates and respond."""
    tracks = cfg.get("tracks") or {}

    for u in tg.get_updates(offset=store.offset or None):
        store.offset = u["update_id"] + 1

        if "callback_query" in u:
            _handle_callback(tg, store, cfg, u["callback_query"])
            continue

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or not chat_id or not text.startswith("/"):
            continue

        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()

        if cmd == "/start":
            store.subscribe(chat_id)
            tg.send(chat_id, WELCOME, keyboard=track_keyboard(tracks))

        elif cmd == "/help":
            tg.send(chat_id, HELP)

        elif cmd == "/stop":
            store.unsubscribe(chat_id)
            tg.send(chat_id, "Stopped. Send /start whenever you want them back.")

        elif cmd == "/track":
            store.subscribe(chat_id)
            tg.send(chat_id, "🎯 Which jobs do you want?",
                    keyboard=track_keyboard(tracks))

        elif cmd == "/level":
            store.subscribe(chat_id)
            tg.send(chat_id, "📊 Which level?", keyboard=level_keyboard())

        elif cmd == "/status":
            tg.send(chat_id,
                    _status_text(store.subscribers().get(str(chat_id)), tracks))

        elif cmd in ("/keywords", "/locations"):
            field = "keywords" if cmd == "/keywords" else "locations"
            store.subscribe(chat_id)
            if arg.lower() in ("clear", "none", "reset"):
                store.set_filter(chat_id, field, [])
                tg.send(chat_id, f"{field.capitalize()} filter cleared.")
            elif arg:
                vals = _parse_list(arg)
                store.set_filter(chat_id, field, vals)
                tg.send(chat_id, f"{field.capitalize()}: <b>{', '.join(vals)}</b>")
            else:
                cur = store.subscribers().get(str(chat_id), {}).get(field, [])
                tg.send(chat_id, f"Current {field}: {', '.join(cur) or 'any'}")

        elif cmd == "/search":
            if not arg:
                tg.send(chat_id, "Usage: <code>/search flutter</code>")
                continue
            tg.send(chat_id, f"🔎 Searching <b>{arg}</b> ...")
            hits = fetch_linkedin(
                arg, geo_id=106155005,          # Egypt
                lookback_seconds=7 * 86400, pages=1, log=log,
            )
            if not hits:
                tg.send(chat_id, "No results - try a different word.")
            for j in hits[:10]:
                j.level = classify_level(j.title)
                tg.send(chat_id, format_job(j, tracks))
                time.sleep(cfg.get("send_delay", 1.2))


# ---------------------------------------------------------------------------
#  One cycle
# ---------------------------------------------------------------------------

def run_once(tg: Telegram, store: Store, cfg: dict, channel: str | None) -> int:
    log("Fetching...")
    jobs = collect(cfg)
    log(f"{len(jobs)} unique jobs collected")

    tracks = cfg.get("tracks") or {}
    fresh = [j for j in jobs if store.is_new(j.id) and passes_filters(j, cfg)]

    by_track = {}
    for j in fresh:
        by_track[j.track] = by_track.get(j.track, 0) + 1
    log(f"{len(fresh)} new after dedup + filters {by_track or ''}")

    cap = cfg.get("max_posts_per_run", 25)
    delay = cfg.get("send_delay", 1.2)
    posted = 0
    channel_ok = True

    for job in fresh[:cap]:
        text = format_job(job, tracks)
        delivered = False

        if channel and channel_ok:
            if tg.send(channel, text) is None:
                channel_ok = False
                log("  ! channel send failed - check CHANNEL_ID and that "
                    "the bot is an admin with Post Messages")
            else:
                delivered = True
            time.sleep(delay)

        for chat_id, sub in list(store.subscribers().items()):
            if matches_subscriber(job, sub):
                if tg.send(chat_id, text) is not None:
                    delivered = True
                time.sleep(delay)

        # Only burn the job id once it actually reached someone.
        if delivered:
            store.mark_seen(job.id)
            posted += 1

    if not channel_ok:
        log("Delivery failed - those jobs were NOT marked seen, "
            "so they will be retried on the next run.")

    for job in fresh[cap:]:
        store.mark_seen(job.id)

    log(f"Posted {posted}")
    return posted


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    cfg = load_config()

    if mode == "test":
        for j in collect(cfg)[:25]:
            ok = "OK  " if passes_filters(j, cfg) else "drop"
            print(f"{ok} [{j.track}/{j.level}] {j.title} - {j.company} "
                  f"- {j.location} - {j.age_text()}")
        return

    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN is not set. Get one from @BotFather.")

    channel = os.environ.get("CHANNEL_ID") or None
    tg = Telegram(token, log=log)
    store = Store(STATE_PATH)

    me = tg.me()
    if me:
        log(f"Running as @{me.get('username')}")

    if mode == "once":
        try:
            handle_commands(tg, store, cfg)
            run_once(tg, store, cfg, channel)
        finally:
            store.save()
        return

    if mode == "serve":
        interval = int(os.environ.get("POLL_SECONDS", "300"))
        last_fetch = 0.0
        log(f"Serve mode, fetching every {interval}s. Ctrl-C to stop.")
        while True:
            try:
                handle_commands(tg, store, cfg)
                if time.time() - last_fetch >= interval:
                    run_once(tg, store, cfg, channel)
                    last_fetch = time.time()
                store.save()
                time.sleep(3)
            except KeyboardInterrupt:
                store.save()
                log("Stopped.")
                return
            except Exception as e:
                log(f"!! cycle error: {e}")
                time.sleep(30)

    sys.exit(f"Unknown mode: {mode!r}. Use once | serve | test.")


if __name__ == "__main__":
    main()
