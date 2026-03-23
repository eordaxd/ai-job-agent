"""
Job scanner — queries public career board APIs and well-known job portals.

Supported sources:
  Greenhouse  → OpenAI, Anthropic, Cohere, Poolside
  Lever       → Mistral
  Custom APIs → Google, Microsoft, AWS
  Workday     → NVIDIA  (best-effort public endpoint)
  Ashby       → Nebius AI
"""

import json
import os
import re
import time
import hashlib
from datetime import date
import urllib.request
import urllib.parse
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
JOBS_FILE = os.path.join(BASE_DIR, "jobs.json")

# ── Slug / board mappings ─────────────────────────────────────────────────────
GREENHOUSE_SLUGS = {
    "OpenAI":    "openai",
    "Anthropic": "anthropic",
    "Cohere":    "cohere",
    "Poolside":  "poolside",
}

LEVER_SLUGS = {
    "Mistral": "mistral",
}

ASHBY_SLUGS = {
    "Nebius AI": "nebius",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _make_id(company, title):
    raw = f"{company}-{title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:60]
    h = hashlib.md5(raw.encode()).hexdigest()[:6]
    return f"{slug}-{h}"


def _match_keywords(text, keywords):
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def _match_location(loc_text, locations):
    loc = loc_text.lower()
    for target in locations:
        # direct substring
        if target.lower() in loc:
            return True
        # "Remote Europe / EMEA / Worldwide" counts for "Remote Europe" or "Remote EMEA"
        if "remote" in target.lower() and "remote" in loc:
            if any(w in loc for w in ["europe", "emea", "worldwide", "global", "anywhere"]):
                return True
    return False


def _job_entry(company, title, location, url, description):
    return {
        "id": _make_id(company, title),
        "title": title,
        "company": company,
        "location": location or "Not specified",
        "url": url or "",
        "description": re.sub(r"<[^>]+>", "", description or "").strip()[:220],
        "date_found": date.today().isoformat(),
    }

# ── Per-source scrapers ───────────────────────────────────────────────────────

def _greenhouse(company, slug, keywords, locations, log):
    found = []
    try:
        data = _get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        )
        total = len(data.get("jobs", []))
        log(f"{company}: {total} total postings on Greenhouse")
        for job in data.get("jobs", []):
            title = job.get("title", "")
            loc   = job.get("location", {}).get("name", "")
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                found.append(_job_entry(
                    company, title, loc,
                    job.get("absolute_url", ""),
                    job.get("content", ""),
                ))
    except Exception as e:
        log(f"{company}: Greenhouse error — {e}")
    return found


def _lever(company, slug, keywords, locations, log):
    found = []
    try:
        data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        log(f"{company}: {len(data)} total postings on Lever")
        for job in data:
            title = job.get("text", "")
            loc   = job.get("categories", {}).get("location", "")
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                found.append(_job_entry(
                    company, title, loc,
                    job.get("hostedUrl", ""),
                    job.get("descriptionPlain", ""),
                ))
    except Exception as e:
        log(f"{company}: Lever error — {e}")
    return found


def _ashby(company, slug, keywords, locations, log):
    found = []
    try:
        payload = json.dumps({"organizationHostedJobsPageName": slug}).encode()
        req = urllib.request.Request(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        jobs = (data.get("data") or {}).get("jobBoard", {}).get("jobPostings", [])
        log(f"{company}: {len(jobs)} total postings on Ashby")
        for job in jobs:
            title = job.get("title", "")
            loc   = job.get("locationName", "") or job.get("isRemote", "")
            if isinstance(loc, bool):
                loc = "Remote" if loc else ""
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                job_id = job.get("id", "")
                found.append(_job_entry(
                    company, title, loc,
                    f"https://jobs.ashbyhq.com/{slug}/{job_id}",
                    job.get("descriptionHtml", ""),
                ))
    except Exception as e:
        log(f"{company}: Ashby error — {e}")
    return found


def _google(keywords, locations, log):
    found = []
    try:
        query = " OR ".join(keywords)
        loc   = " OR ".join(locations)
        url = (
            "https://careers.google.com/api/jobs/list?"
            + urllib.parse.urlencode({
                "q": query,
                "location": loc,
                "page_size": 50,
                "page": 1,
            })
        )
        data = _get(url)
        jobs = data.get("jobs", [])
        log(f"Google: {len(jobs)} matching postings")
        for job in jobs:
            title   = job.get("title", "")
            locs    = ", ".join(job.get("locations", []))
            job_url = "https://careers.google.com/jobs/results/" + job.get("job_id", "")
            found.append(_job_entry("Google", title, locs, job_url, job.get("summary", "")))
    except Exception as e:
        log(f"Google: careers API error — {e}")
    return found


def _microsoft(keywords, locations, log):
    found = []
    try:
        for kw in keywords[:4]:   # limit to avoid rate limiting
            params = urllib.parse.urlencode({
                "q": kw,
                "l": " ".join(locations),
                "pg": 1,
                "pgSz": 20,
                "lc": "en_US",
                "exp": "8",   # experience filter
            })
            url = f"https://gcsservices.careers.microsoft.com/search/api/v1/search?{params}"
            data = _get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)",
                "Accept": "application/json",
            })
            jobs = data.get("operationResult", {}).get("result", {}).get("jobs", [])
            for job in jobs:
                title    = job.get("title", "")
                loc      = job.get("primaryWorkLocation", "")
                job_id   = job.get("jobId", "")
                job_url  = f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
                desc     = job.get("properties", {}).get("description", "")
                if _match_location(loc, locations):
                    found.append(_job_entry("Microsoft", title, loc, job_url, desc))
            time.sleep(0.3)
        log(f"Microsoft: {len(found)} matching postings")
    except Exception as e:
        log(f"Microsoft: careers API error — {e}")
    return found


def _amazon(keywords, locations, log):
    found = []
    try:
        for kw in keywords[:4]:
            params = urllib.parse.urlencode({
                "base_query": kw,
                "loc_query": "Spain",
                "category": "",
                "result_limit": 20,
            })
            url = f"https://www.amazon.jobs/en/search.json?{params}"
            data = _get(url)
            for job in data.get("jobs", []):
                title = job.get("title", "")
                loc   = job.get("normalized_location", "")
                jurl  = "https://www.amazon.jobs" + job.get("job_path", "")
                desc  = job.get("description_short", "")
                if _match_location(loc, locations):
                    found.append(_job_entry("AWS", title, loc, jurl, desc))
            time.sleep(0.3)
        log(f"AWS: {len(found)} matching postings")
    except Exception as e:
        log(f"AWS: jobs API error — {e}")
    return found


def _nvidia(keywords, locations, log):
    """NVIDIA uses Workday — query their public search endpoint."""
    found = []
    try:
        for kw in keywords[:3]:
            payload = json.dumps({
                "appliedFacets": {},
                "limit": 20,
                "offset": 0,
                "searchText": kw,
            }).encode()
            req = urllib.request.Request(
                "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)",
                },
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
            for job in data.get("jobPostings", []):
                title = job.get("title", "")
                loc   = job.get("locationsText", "")
                path  = job.get("externalPath", "")
                jurl  = f"https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite{path}"
                if _match_location(loc, locations):
                    found.append(_job_entry("NVIDIA", title, loc, jurl, ""))
            time.sleep(0.3)
        log(f"NVIDIA: {len(found)} matching postings")
    except Exception as e:
        log(f"NVIDIA: Workday error — {e}")
    return found

# ── Main entry point ──────────────────────────────────────────────────────────

def run_scan(progress_callback=None):
    """
    Scan all configured companies and merge results into jobs.json.
    progress_callback(message: str) is called with status updates.
    Returns (new_count, total_count).
    """
    def log(msg):
        if progress_callback:
            progress_callback(msg)

    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    companies = cfg.get("companies", [])
    keywords  = cfg.get("roles", [])
    locations = cfg.get("locations", [])

    log(f"Starting scan: {len(companies)} companies, {len(keywords)} role keywords")

    all_found = []

    for company in companies:
        log(f"Searching {company}...")
        if company in GREENHOUSE_SLUGS:
            all_found += _greenhouse(company, GREENHOUSE_SLUGS[company], keywords, locations, log)
        elif company in LEVER_SLUGS:
            all_found += _lever(company, LEVER_SLUGS[company], keywords, locations, log)
        elif company in ASHBY_SLUGS:
            all_found += _ashby(company, ASHBY_SLUGS[company], keywords, locations, log)
        elif company == "Google":
            all_found += _google(keywords, locations, log)
        elif company == "Microsoft":
            all_found += _microsoft(keywords, locations, log)
        elif company in ("AWS", "Amazon"):
            all_found += _amazon(keywords, locations, log)
        elif company == "NVIDIA":
            all_found += _nvidia(keywords, locations, log)
        else:
            # Unknown company — try Greenhouse with a slugified name
            slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
            log(f"{company}: trying Greenhouse slug '{slug}'")
            all_found += _greenhouse(company, slug, keywords, locations, log)

    # ── Merge with existing jobs ──────────────────────────────────────────────
    if os.path.exists(JOBS_FILE):
        with open(JOBS_FILE) as f:
            existing = json.load(f)
    else:
        existing = {"last_scan": None, "jobs": []}

    existing_ids = {j["id"] for j in existing.get("jobs", [])}
    new_jobs = [j for j in all_found if j["id"] not in existing_ids]

    merged = existing.get("jobs", []) + new_jobs

    # Drop duplicates by id (keep newest)
    seen, deduped = set(), []
    for j in reversed(merged):
        if j["id"] not in seen:
            seen.add(j["id"])
            deduped.append(j)
    deduped.reverse()

    result = {
        "last_scan": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "jobs": deduped,
    }

    with open(JOBS_FILE, "w") as f:
        json.dump(result, f, indent=2)

    log(f"Done — {len(new_jobs)} new jobs added, {len(deduped)} total in database")
    return len(new_jobs), len(deduped)


if __name__ == "__main__":
    run_scan(print)
