"""Job sources. Each source is a function returning a list of Job in the common format below."""
import html as html_lib
import re
import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import httpx

from pipeline.settings import CONFIG

# Honest identification; no browser impersonation.
USER_AGENT = "job(less)-bot/0.1 (personal job search tool, low volume)"
REQUEST_DELAY_SECONDS = 1.5


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str            # where we found it: 'jsearch', 'greenhouse', 'workday', 'gmail', ...
    apply_system: str      # where you'd apply: 'workday', 'greenhouse', 'lever', 'manual', ...
    date_found: str = field(default_factory=lambda: date.today().isoformat())

    def dedupe_key(self) -> str:
        # Same job from two sources has different URLs, so match on company + title + city.
        city = self.location.split(",")[0]
        return "|".join(_norm(x) for x in (self.company, self.title, city))


_SUFFIXES = re.compile(r"\b(inc|ltd|limited|corp|corporation|co|llc|ulc|canada)\b")


def _norm(text: str) -> str:
    text = _SUFFIXES.sub(" ", (text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def apply_system_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for marker, system in [("myworkdayjobs", "workday"), ("greenhouse", "greenhouse"), ("lever.co", "lever"),
                           ("successfactors", "successfactors"), ("taleo", "taleo"), ("icims", "icims")]:
        if marker in host:
            return system
    return "manual"


def is_local(location: str) -> bool:
    loc = (location or "").lower()
    if re.search(r",\s*(?!on\b)[a-z]{2}\s*$", loc):  # ends in another province/state code, e.g. "Aurora, CO"
        return False
    if "remote" in loc or "teletravail" in loc:  # remote only counts when it's remote within Canada/Ontario
        return any(w in loc for w in ("canada", "ontario", ", on", " can"))
    return any(area.lower() in loc for area in CONFIG["search"]["areas"])


_last_request = 0.0


def http() -> httpx.Client:
    def wait(_request):
        global _last_request
        pause = REQUEST_DELAY_SECONDS - (time.monotonic() - _last_request)
        if pause > 0:
            time.sleep(pause)
        _last_request = time.monotonic()

    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True,
                        event_hooks={"request": [wait]})


def html_to_text(html: str) -> str:
    text = html_lib.unescape(html or "")  # Greenhouse sends HTML-escaped HTML
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html_lib.unescape(text)).strip()
