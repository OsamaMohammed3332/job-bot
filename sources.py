"""
Job sources.

Primary source is LinkedIn's PUBLIC guest endpoint — the same one that powers
the "see more jobs" list you get when browsing linkedin.com/jobs while logged
out. No login, no cookies, no API key. It returns a chunk of HTML with ~10 job
cards per page.

Secondary sources are free public JSON APIs (Remotive / RemoteOK / Arbeitnow)
which cover remote jobs that never get posted to LinkedIn, and which keep the
bot useful on the days LinkedIn rate-limits you.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests
from bs4 import BeautifulSoup

GUEST_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# Rotating UA pool. LinkedIn fingerprints aggressively on a static UA.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


@dataclass
class Job:
    """One normalised job posting."""

    id: str                      # stable unique key used for dedup
    title: str
    company: str
    location: str
    url: str
    source: str = "LinkedIn"
    posted_at: datetime | None = None   # when the job went live (UTC)
    tags: list[str] = field(default_factory=list)
    track: str = ""                     # "flutter" | "odoo" | ...
    level: str = ""                     # "junior" | "mid" | "senior"

    def age_text(self) -> str:
        """Human 'x minutes ago' string, like the channel you showed."""
        if not self.posted_at:
            return "recently"
        delta = datetime.now(timezone.utc) - self.posted_at
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''} ago"

    def haystack(self) -> str:
        return f"{self.title} {self.company} {self.location} {' '.join(self.tags)}".lower()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    )
    return s


# --------------------------------------------------------------------------
#  LinkedIn guest endpoint
# --------------------------------------------------------------------------

def _job_id_from_card(card) -> str | None:
    """LinkedIn puts the numeric posting id in a few different places
    depending on which A/B variant of the markup you get back."""
    for attr in ("data-entity-urn", "data-id"):
        holder = card.find(attrs={attr: True}) or (
            card if card.has_attr(attr) else None
        )
        if holder:
            raw = holder[attr]
            m = re.search(r"(\d{6,})", raw)
            if m:
                return m.group(1)

    link = card.find("a", href=True)
    if link:
        m = re.search(r"-(\d{6,})(?:\?|$)", link["href"])
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------
#  Seniority classification
# --------------------------------------------------------------------------
#
# LinkedIn's own experience filter (f_E) can only be applied per query, so
# using it would mean running every search three times over. Instead we fetch
# once and read the level off the title. It's a heuristic, not gospel: a
# posting titled plain "Flutter Developer" lands in "mid" whatever the body
# text says. That is the honest trade for covering every level in one pass.

_SENIOR_MARKERS = [
    "senior", "sr.", "sr ", "lead", "principal", "staff engineer",
    "architect", "head of", "expert", "iii", " iv", "manager",
    "كبير", "خبير",
]
_JUNIOR_MARKERS = [
    "junior", "jr.", "jr ", "intern", "internship", "trainee",
    "entry level", "entry-level", "graduate", "fresh grad", "fresher",
    "مبتدئ", "متدرب", "حديث",
]


def classify_level(title: str) -> str:
    """Return 'junior', 'senior', or 'mid' based on the job title."""
    t = f" {title.lower()} "
    if any(m in t for m in _JUNIOR_MARKERS):
        return "junior"
    if any(m in t for m in _SENIOR_MARKERS):
        return "senior"
    return "mid"


_REL_RE = re.compile(
    r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_REL_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    "year": 31536000,
}


def parse_relative_time(text: str | None) -> datetime | None:
    """Turn LinkedIn's '53 minutes ago' into a real timestamp.

    This is the ONLY accurate source of posting time on a guest card. The
    <time datetime="..."> attribute is date-only (no clock time), so reading
    it gives midnight UTC and makes every job posted today look hours old.
    Also handles 'Reposted 2 hours ago'.
    """
    if not text:
        return None
    m = _REL_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return datetime.now(timezone.utc) - timedelta(seconds=n * _REL_SECONDS[unit])


def parse_guest_html(html: str) -> list[Job]:
    """Parse the HTML fragment returned by the guest endpoint.

    Kept as its own function so it can be unit-tested against a saved
    fixture without hitting the network.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.find_all("li")
    if not cards:
        cards = soup.find_all("div", class_=re.compile(r"base-card"))

    jobs: list[Job] = []
    for card in cards:
        title_el = card.find(class_=re.compile(r"base-search-card__title"))
        company_el = card.find(class_=re.compile(r"base-search-card__subtitle"))
        loc_el = card.find(class_=re.compile(r"job-search-card__location"))
        link_el = card.find("a", href=True)
        if not (title_el and link_el):
            continue

        job_id = _job_id_from_card(card)
        if not job_id:
            continue

        # Prefer the canonical /jobs/view/<id> URL — the raw href carries
        # tracking params and sometimes a country subdomain.
        url = f"https://www.linkedin.com/jobs/view/{job_id}"

        posted_at = None
        time_el = card.find("time")
        if time_el:
            # Prefer the visible "53 minutes ago" text — it's the only
            # source with clock precision.
            posted_at = parse_relative_time(time_el.get_text(strip=True))

            if posted_at is None and time_el.get("datetime"):
                try:
                    d = datetime.fromisoformat(time_el["datetime"]).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    d = None
                if d is not None:
                    # A date-only value for TODAY would render as midnight,
                    # i.e. "14 hours ago" for a job posted 5 minutes ago.
                    # Rather than lie, report the age as unknown.
                    if d.date() == datetime.now(timezone.utc).date():
                        posted_at = None
                    else:
                        posted_at = d

        jobs.append(
            Job(
                id=f"li:{job_id}",
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "—",
                location=loc_el.get_text(strip=True) if loc_el else "—",
                url=url,
                source="LinkedIn",
                posted_at=posted_at,
            )
        )
    return jobs


def fetch_linkedin(
    keywords: str,
    location: str = "",
    *,
    geo_id: str | int | None = None,
    lookback_seconds: int = 7200,
    exp: Iterable[int] | None = None,
    pages: int = 2,
    log=print,
) -> list[Job]:
    """Query the LinkedIn guest endpoint. Returns [] on rate-limit rather
    than raising, so one bad query never kills the whole run.

    ALWAYS pass geo_id when you can. A plain location string that LinkedIn
    does not recognise (notably "Worldwide") is silently discarded and the
    endpoint falls back to geolocating the CALLER'S IP — which on a GitHub
    runner means US jobs, not what you asked for.

    Note there is deliberately no remote parameter. The guest endpoint
    accepts f_WT=2 but ignores it: identical result sets come back with and
    without it (verified). Remote filtering has to happen elsewhere.
    """
    sess = _session()
    out: list[Job] = []

    for page in range(pages):
        params = {
            "keywords": keywords,
            "f_TPR": f"r{lookback_seconds}",
            "sortBy": "DD",           # date descending = newest first
            "start": page * 10,
        }
        if geo_id:
            params["geoId"] = str(geo_id)
        else:
            params["location"] = location
        if exp:
            params["f_E"] = ",".join(str(e) for e in exp)

        try:
            r = sess.get(GUEST_URL, params=params, timeout=25)
        except requests.RequestException as e:
            log(f"  ! network error [{keywords} / {location}]: {e}")
            break

        if r.status_code == 429:
            log(f"  ! rate limited (429) on [{keywords} / {location}]")
            time.sleep(5)
            break
        if r.status_code != 200 or not r.text.strip():
            break

        batch = parse_guest_html(r.text)
        out.extend(batch)
        if len(batch) < 10:
            break                     # last page

        time.sleep(random.uniform(1.5, 3.0))   # be polite, avoid 429

    return out


# --------------------------------------------------------------------------
#  Free JSON APIs (remote jobs)
# --------------------------------------------------------------------------

def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def fetch_remotive(log=print) -> list[Job]:
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "software-dev", "limit": 60},
            timeout=25,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )
        r.raise_for_status()
        data = r.json().get("jobs", [])
    except Exception as e:
        log(f"  ! remotive failed: {e}")
        return []

    return [
        Job(
            id=f"rmtv:{j['id']}",
            title=j.get("title", "").strip(),
            company=j.get("company_name", "—").strip(),
            location=j.get("candidate_required_location") or "Remote",
            url=j.get("url", ""),
            source="Remotive",
            posted_at=_parse_iso(j.get("publication_date")),
            tags=j.get("tags", []) or [],
        )
        for j in data
        if j.get("id") and j.get("url")
    ]


def fetch_remoteok(log=print) -> list[Job]:
    try:
        r = requests.get(
            "https://remoteok.com/api",
            timeout=25,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"  ! remoteok failed: {e}")
        return []

    jobs = []
    for j in data:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue
        jobs.append(
            Job(
                id=f"rok:{j['id']}",
                title=j["position"].strip(),
                company=(j.get("company") or "—").strip(),
                location=j.get("location") or "Remote",
                url=j.get("url") or f"https://remoteok.com/l/{j['id']}",
                source="RemoteOK",
                posted_at=_parse_iso(j.get("date")),
                tags=j.get("tags", []) or [],
            )
        )
    return jobs


def fetch_arbeitnow(log=print) -> list[Job]:
    try:
        r = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            timeout=25,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        log(f"  ! arbeitnow failed: {e}")
        return []

    jobs = []
    for j in data:
        slug = j.get("slug")
        if not slug:
            continue
        created = j.get("created_at")
        posted = (
            datetime.fromtimestamp(created, tz=timezone.utc)
            if isinstance(created, (int, float))
            else None
        )
        jobs.append(
            Job(
                id=f"arb:{slug}",
                title=(j.get("title") or "").strip(),
                company=(j.get("company_name") or "—").strip(),
                location="Remote" if j.get("remote") else (j.get("location") or "—"),
                url=j.get("url", ""),
                source="Arbeitnow",
                posted_at=posted,
                tags=j.get("tags", []) or [],
            )
        )
    return jobs
