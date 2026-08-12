#!/usr/bin/env python3
"""
Telegram job bot — LinkedIn (Egypt) + remote jobs worldwide.

Modes
-----
  python bot.py once     Fetch new jobs, post them, handle any pending
                         commands, save state, exit.  <- GitHub Actions
  python bot.py serve    Same thing on a loop, plus instant command
                         replies via long-polling.    <- VPS / Railway
  python bot.py test     Fetch and print jobs. Sends nothing to Telegram.

Environment
-----------
  BOT_TOKEN    required — from @BotFather
  CHANNEL_ID   optional — @yourchannel or -100123...; omit to skip channel
  POLL_SECONDS optional — serve-mode interval, default 300
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import yaml

from sources import (
    Job,
    fetch_arbeitnow,
    fetch_linkedin,
    fetch_remoteok,
    fetch_remotive,
)
from store import Store
from tg import HELP, Telegram, format_job

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")
STATE_PATH = os.path.join(HERE, "state.json")


def log(*a):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}]", *a, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
#  Collect + filter
# ---------------------------------------------------------------------------

def passes_filters(job: Job, filters: dict) -> bool:
    title = job.title.lower()
    location = job.location.lower()

    blocked = [c.lower() for c in filters.get("blocked_companies") or []]
    if any(b in job.company.lower() for b in blocked):
        return False

    # Drop roles advertised for markets you don't want to be hired into.
    for loc in filters.get("blocked_locations") or []:
        if loc.lower() in location:
            return False

    # The geography gate.
    #
    # A job is only useful if it is either (a) where you live, or (b) actually
    # remote. LinkedIn's guest endpoint ignores its own remote filter (f_WT)
    # and the job cards carry no work-type field, so an on-site role in Malmo
    # is indistinguishable from a remote one at the source. This gate is the
    # backstop: anything outside your home country must say remote somewhere
    # in its location, or it gets dropped.
    home = [h.lower() for h in filters.get("home_locations") or []]
    markers = [m.lower() for m in filters.get("remote_markers") or []]
    if home or markers:
        at_home = any(h in location for h in home)
        is_remote = any(m in location for m in markers)
        if not (at_home or is_remote):
            return False

    bad = [w.lower() for w in filters.get("title_must_not_include") or []]
    if any(w in title for w in bad):
        return False

    # Two different include lists, because the sources differ in quality:
    #
    #   LinkedIn results already came from a Flutter/mobile-specific search
    #   query, so the title itself doesn't have to prove relevance. Using the
    #   strict list here would throw away Arabic-language postings like
    #   "مهندس برمجيات" that are genuinely Flutter roles.
    #
    #   The remote-job APIs return all of software dev, so those need the
    #   strict mobile-only list.
    if job.source == "LinkedIn":
        good = filters.get("title_must_include_linkedin") or []
    else:
        good = filters.get("title_must_include") or []

    good = [w.lower() for w in good]
    if good and not any(w in title for w in good):
        return False

    return True


def collect(cfg: dict) -> list[Job]:
    """Run every configured search + extra source, merge, dedupe, sort."""
    lookback = cfg.get("lookback_seconds", 7200)
    jobs: list[Job] = []

    for s in cfg.get("searches", []):
        kw = s.get("keywords", "")
        loc = s.get("location", "")
        found = fetch_linkedin(
            kw,
            loc,
            geo_id=s.get("geo_id"),
            lookback_seconds=lookback,
            exp=s.get("exp"),
            log=log,
        )
        log(f"  LinkedIn [{kw or 'any'} @ {loc or s.get('geo_id')}] -> {len(found)}")
        jobs.extend(found)

    extra = cfg.get("extra_sources") or {}
    cutoff = time.time() - lookback
    for name, fn in (
        ("remotive", fetch_remotive),
        ("remoteok", fetch_remoteok),
        ("arbeitnow", fetch_arbeitnow),
    ):
        if not extra.get(name):
            continue
        found = [
            j for j in fn(log=log)
            if j.posted_at is None or j.posted_at.timestamp() >= cutoff
        ]
        log(f"  {name} -> {len(found)}")
        jobs.extend(found)

    # Dedupe by id, then by (title, company) to catch the same role
    # cross-posted to two sources.
    seen_ids, seen_pairs, unique = set(), set(), []
    for j in jobs:
        pair = (j.title.lower().strip(), j.company.lower().strip())
        if j.id in seen_ids or pair in seen_pairs:
            continue
        seen_ids.add(j.id)
        seen_pairs.add(pair)
        unique.append(j)

    # Newest first — so if we hit max_posts_per_run we keep the freshest.
    unique.sort(
        key=lambda j: j.posted_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return unique


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------

def _parse_list(arg: str) -> list[str]:
    return [p.strip().lower() for p in arg.replace(",", " ").split() if p.strip()]


def handle_commands(tg: Telegram, store: Store, cfg: dict) -> None:
    """Drain pending Telegram updates and respond to commands."""
    updates = tg.get_updates(offset=store.offset or None)
    for u in updates:
        store.offset = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or not chat_id or not text.startswith("/"):
            continue

        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()      # strip @botname in groups
        arg = arg.strip()

        if cmd in ("/start", "/help"):
            store.subscribe(chat_id)
            tg.send(chat_id, HELP)

        elif cmd == "/stop":
            store.unsubscribe(chat_id)
            tg.send(chat_id, "Unsubscribed. Send /start to turn alerts back on.")

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
                tg.send(chat_id, f"Current {field}: {', '.join(cur) or 'none'}")

        elif cmd == "/status":
            sub = store.subscribers().get(str(chat_id))
            if not sub:
                tg.send(chat_id, "Not subscribed. Send /start.")
            else:
                tg.send(
                    chat_id,
                    "Subscribed ✅\n"
                    f"Keywords: {', '.join(sub['keywords']) or 'all'}\n"
                    f"Locations: {', '.join(sub['locations']) or 'all'}",
                )

        elif cmd == "/search":
            if not arg:
                tg.send(chat_id, "Usage: <code>/search flutter egypt</code>")
                continue
            tg.send(chat_id, f"🔎 Searching <b>{arg}</b> …")
            # Last word is treated as a country if we know its geoId.
            # geoIds are used rather than location strings because an
            # unrecognised string makes LinkedIn geolocate the runner's IP.
            GEO = {
                "egypt": 106155005,
                "uae": 104305776,
                "saudi": 100459316,
                "germany": 101282230,
                "uk": 101165590,
                "usa": 103644278,
            }
            words = arg.split()
            geo = GEO["egypt"]
            kw = arg
            if len(words) > 1 and words[-1].lower() in GEO:
                kw = " ".join(words[:-1])
                geo = GEO[words[-1].lower()]
            hits = fetch_linkedin(
                kw,
                geo_id=geo,
                lookback_seconds=7 * 86400,
                pages=1,
                log=log,
            )
            if not hits:
                tg.send(chat_id, "No results (or LinkedIn rate-limited us — try again in a minute).")
            for j in hits[:10]:
                tg.send(chat_id, format_job(j))
                time.sleep(cfg.get("send_delay", 1.2))


def matches_subscriber(job: Job, sub: dict) -> bool:
    hay = job.haystack()
    kws = sub.get("keywords") or []
    locs = sub.get("locations") or []
    if kws and not any(k in hay for k in kws):
        return False
    if locs and not any(l in job.location.lower() or l in hay for l in locs):
        return False
    return True


# ---------------------------------------------------------------------------
#  One cycle
# ---------------------------------------------------------------------------

def run_once(tg: Telegram, store: Store, cfg: dict, channel: str | None) -> int:
    log("Fetching…")
    jobs = collect(cfg)
    log(f"{len(jobs)} unique jobs collected")

    fresh = [
        j for j in jobs
        if store.is_new(j.id) and passes_filters(j, cfg.get("filters") or {})
    ]
    log(f"{len(fresh)} new after dedup + filters")

    cap = cfg.get("max_posts_per_run", 25)
    delay = cfg.get("send_delay", 1.2)
    posted = 0

    channel_ok = True

    for job in fresh[:cap]:
        text = format_job(job)
        delivered = False

        if channel and channel_ok:
            if tg.send(channel, text) is None:
                # Almost always a bad CHANNEL_ID or the bot isn't an admin.
                # Stop hammering it for the rest of this run.
                channel_ok = False
                log("  ! channel send failed — check CHANNEL_ID and that "
                    "the bot is an admin with Post Messages")
            else:
                delivered = True
            time.sleep(delay)

        for chat_id, sub in list(store.subscribers().items()):
            if matches_subscriber(job, sub):
                if tg.send(chat_id, text) is not None:
                    delivered = True
                time.sleep(delay)

        # Only burn the job id once it actually reached someone. Otherwise a
        # misconfigured run would silently swallow every job it found.
        if delivered:
            store.mark_seen(job.id)
            posted += 1

    if not channel_ok:
        log("Delivery failed — those jobs were NOT marked seen, "
            "so they'll be retried on the next run.")

    # Mark the overflow as seen, otherwise the next run posts stale jobs.
    for job in fresh[cap:]:
        store.mark_seen(job.id)

    log(f"Posted {posted}")
    return posted


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    cfg = load_config()

    if mode == "test":
        for j in collect(cfg)[:15]:
            ok = "✓" if passes_filters(j, cfg.get("filters") or {}) else "✗"
            print(f"{ok} [{j.source}] {j.title} — {j.company} — {j.location} — {j.age_text()}")
            print(f"   {j.url}")
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
            store.save()          # always persist, even on a crash
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
            except Exception as e:                     # keep the bot alive
                log(f"!! cycle error: {e}")
                time.sleep(30)

    sys.exit(f"Unknown mode: {mode!r}. Use once | serve | test.")


if __name__ == "__main__":
    main()
