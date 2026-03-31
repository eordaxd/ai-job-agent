"""
Job scanner — queries public career board APIs and well-known job portals.

Supported sources:
  Greenhouse  → Anthropic (confirmed), + auto-detect for unknown companies
  Lever       → Mistral (confirmed),   + auto-detect
  Ashby       → auto-detect
  Workday     → NVIDIA
  Custom APIs → Google Careers, Microsoft Careers, Amazon Jobs
  LinkedIn    → fallback for Google / Microsoft when their APIs are down
"""

import json
import os
import re
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
JOBS_FILE   = os.path.join(BASE_DIR, "jobs.json")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _make_id(company, title):
    raw  = f"{company}-{title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:60]
    h    = hashlib.md5(raw.encode()).hexdigest()[:6]
    return f"{slug}-{h}"


def _match_keywords(text, keywords):
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def _match_location(loc_text, locations):
    """
    Accept a job if its location matches any target.

    Rules:
    - Direct substring: "Spain" in "Madrid, Spain" → yes
    - Spain aliases: ESP, Madrid, Barcelona, etc.
    - Remote: accept any "Remote" position UNLESS explicitly US-only.
      Many European-eligible roles simply say "Remote" without specifying
      a continent, so we include them and let the user filter.
    """
    loc = loc_text.lower()

    # Hard exclusion: explicit US/North-America-only language
    us_only_markers = [
        "united states only", "us only", "usa only",
        "north america only", "must be located in the us",
        "must reside in the us",
    ]
    if any(m in loc for m in us_only_markers):
        return False

    for target in locations:
        t = target.lower()

        # 1. Direct substring match  (e.g. "spain" ⊂ "madrid, spain")
        if t in loc:
            return True

        # 2. Spain: also match country code and major cities
        if "spain" in t:
            if any(kw in loc for kw in ["spain", "esp", "madrid", "barcelona",
                                         "valencia", "bilbao", "seville", "sevilla"]):
                return True

        # 3. "Remote …" target: accept any remote role without US restriction
        if "remote" in t and "remote" in loc:
            return True

    return False


def _job_entry(company, title, location, url, description=""):
    return {
        "id":          _make_id(company, title),
        "title":       title,
        "company":     company,
        "location":    location or "Not specified",
        "url":         url or "",
        "description": re.sub(r"<[^>]+>", "", description or "").strip()[:220],
        "date_found":  date.today().isoformat(),
    }


# ── Generic platform scrapers ─────────────────────────────────────────────────
# Return None  → HTTP 404 / org not found (try next platform)
# Return []    → platform found, zero keyword+location matches
# Return [...]  → matches found

def _greenhouse(company, slug, keywords, locations, log):
    try:
        data  = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        total = len(data.get("jobs", []))
        log(f"{company}: {total} total postings on Greenhouse (slug: {slug})")
        found = []
        for job in data.get("jobs", []):
            title = job.get("title", "")
            loc   = job.get("location", {}).get("name", "")
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                found.append(_job_entry(
                    company, title, loc,
                    job.get("absolute_url", ""),
                    job.get("content", ""),
                ))
        return found
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log(f"{company}: Greenhouse HTTP {e.code}")
        return []
    except Exception as e:
        log(f"{company}: Greenhouse error — {e}")
        return []


def _lever(company, slug, keywords, locations, log):
    try:
        data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if not isinstance(data, list):
            return None
        log(f"{company}: {len(data)} total postings on Lever (slug: {slug})")
        found = []
        for job in data:
            title = job.get("text", "")
            loc   = job.get("categories", {}).get("location", "")
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                found.append(_job_entry(
                    company, title, loc,
                    job.get("hostedUrl", ""),
                    job.get("descriptionPlain", ""),
                ))
        return found
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log(f"{company}: Lever HTTP {e.code}")
        return []
    except Exception as e:
        log(f"{company}: Lever error — {e}")
        return []


def _ashby(company, slug, keywords, locations, log):
    try:
        payload = json.dumps({"organizationHostedJobsPageName": slug}).encode()
        req = urllib.request.Request(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "Mozilla/5.0 (compatible; JobAgent/1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())

        job_board = (data.get("data") or {}).get("jobBoard")
        if job_board is None:
            return None  # Org not found on Ashby

        jobs = job_board.get("jobPostings", [])
        log(f"{company}: {len(jobs)} total postings on Ashby (slug: {slug})")
        found = []
        for job in jobs:
            title  = job.get("title", "")
            loc    = job.get("locationName", "")
            if not loc and job.get("isRemote"):
                loc = "Remote"
            if _match_keywords(title, keywords) and _match_location(loc, locations):
                found.append(_job_entry(
                    company, title, loc,
                    f"https://jobs.ashbyhq.com/{slug}/{job.get('id', '')}",
                ))
        return found
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return None
        log(f"{company}: Ashby HTTP {e.code}")
        return []
    except Exception as e:
        log(f"{company}: Ashby error — {e}")
        return []


def _try_platforms(company, keywords, locations, log):
    """
    For companies with unknown/unverified job boards:
    try Greenhouse → Lever → Ashby with common slug variations.
    Stops at the first platform that responds (even if 0 matches).
    """
    # Build slug variations; exclude any that contain whitespace (invalid in URLs)
    raw_slugs = [
        company,                                                    # "OpenAI"  (Ashby is case-sensitive)
        re.sub(r"[^a-zA-Z0-9]+", "", company),                    # "OpenAI"  (no spaces)
        re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),   # "open-ai"
        re.sub(r"[^a-z0-9]+", "",  company.lower()),              # "openai"
        company.lower().replace(" ai", "").replace(" ", "-"),     # "nebius"
        company.lower().replace(" ai", "").replace(" ", ""),      # "nebius"
        company.replace(" ", ""),                                  # "PoolsideAI"
    ]
    slugs = list(dict.fromkeys(s for s in raw_slugs if s and " " not in s))

    for slug in slugs:
        for fn in (_greenhouse, _lever, _ashby):
            result = fn(company, slug, keywords, locations, log)
            if result is not None:          # platform recognised this slug
                if result:
                    log(f"{company}: {len(result)} matching jobs found")
                return result
        time.sleep(0.2)

    log(f"{company}: not found on Greenhouse/Lever/Ashby — falling back to LinkedIn")
    return _linkedin_search(company, keywords, locations, log)


# ── Company-specific scrapers ─────────────────────────────────────────────────

def _primary_loc(locations):
    """First non-remote location string to use in API queries."""
    for loc in locations:
        if "remote" not in loc.lower():
            return loc
    return locations[0] if locations else "Madrid"


def _google(keywords, locations, log):
    """Try Google Careers API; fall back to LinkedIn guest search."""
    loc_q = _primary_loc(locations)
    for url_tpl in [
        "https://careers.google.com/api/jobs/list?page_size=20&q={q}&location={loc}&sort_by=date",
        "https://careers.google.com/api/jobs/list/?page_size=20&q={q}&location={loc}",
    ]:
        try:
            url  = url_tpl.format(
                q=urllib.parse.quote(" ".join(keywords[:3])),
                loc=urllib.parse.quote(loc_q),
            )
            data = _get(url)
            jobs = data.get("jobs", [])
            log(f"Google: {len(jobs)} jobs from careers API")
            found = []
            for job in jobs:
                title = job.get("title", "")
                locs  = ", ".join(job.get("locations", []))
                jurl  = "https://careers.google.com/jobs/results/" + job.get("job_id", "")
                found.append(_job_entry("Google", title, locs, jurl, job.get("summary", "")))
            return found
        except urllib.error.HTTPError as e:
            log(f"Google: careers API returned {e.code}, trying fallback…")
        except Exception as e:
            log(f"Google: careers API error — {e}, trying fallback…")

    return _linkedin_search("Google", keywords, locations, log)


def _microsoft(keywords, locations, log):
    """Try Microsoft Careers API; fall back to LinkedIn guest search."""
    found = []
    loc_q = _primary_loc(locations)
    try:
        for kw in keywords[:4]:
            params = urllib.parse.urlencode({
                "q": kw, "l": loc_q,
                "pg": 1, "pgSz": 20, "lc": "en_US",
            })
            url  = f"https://gcsservices.careers.microsoft.com/search/api/v1/search?{params}"
            data = _get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)",
                "Accept":     "application/json",
                "Origin":     "https://careers.microsoft.com",
                "Referer":    "https://careers.microsoft.com/",
            })
            for job in data.get("operationResult", {}).get("result", {}).get("jobs", []):
                title  = job.get("title", "")
                loc    = job.get("primaryWorkLocation", "")
                job_id = job.get("jobId", "")
                jurl   = f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
                if _match_location(loc, locations):
                    found.append(_job_entry("Microsoft", title, loc, jurl))
            time.sleep(0.3)
        log(f"Microsoft: {len(found)} matching postings")
        return found
    except Exception as e:
        log(f"Microsoft: careers API error — {e}, trying fallback…")
        return _linkedin_search("Microsoft", keywords, locations, log)


def _nvidia(keywords, locations, log):
    """NVIDIA Workday public endpoint."""
    found = []
    try:
        for kw in keywords[:3]:
            payload = json.dumps({
                "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": kw,
            }).encode()
            req = urllib.request.Request(
                "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent":   "Mozilla/5.0 (compatible; JobAgent/1.0)",
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
                    found.append(_job_entry("NVIDIA", title, loc, jurl))
            time.sleep(0.3)
        log(f"NVIDIA: {len(found)} matching postings from Workday")
    except Exception as e:
        log(f"NVIDIA: Workday error — {e}")
    return found


def _amazon(keywords, locations, log):
    """Amazon Jobs public API (covers AWS roles)."""
    found = []
    primary = _primary_loc(locations)
    loc_queries = list(dict.fromkeys([primary, "Madrid"]))  # deduplicated
    try:
        for kw in keywords[:4]:
            for loc_q in loc_queries:
                params = urllib.parse.urlencode({
                    "base_query": kw, "loc_query": loc_q, "result_limit": 20,
                })
                data = _get(f"https://www.amazon.jobs/en/search.json?{params}")
                for job in data.get("jobs", []):
                    title = job.get("title", "")
                    loc   = job.get("normalized_location", "")
                    jurl  = "https://www.amazon.jobs" + job.get("job_path", "")
                    found.append(_job_entry("AWS", title, loc, jurl,
                                            job.get("description_short", "")))
                time.sleep(0.3)
        # Deduplicate
        seen, deduped = set(), []
        for j in found:
            if j["id"] not in seen:
                seen.add(j["id"])
                deduped.append(j)
        log(f"AWS: {len(deduped)} postings retrieved")
        return deduped
    except Exception as e:
        log(f"AWS: jobs API error — {e}")
    return found


def _linkedin_search(company, keywords, locations, log):
    """
    LinkedIn guest job search — no authentication required.
    Used as fallback for companies whose own APIs are unavailable.
    """
    found = []
    try:
        li_locs = locations[:2] if locations else ["Madrid"]
        for kw in keywords[:2]:
            for loc in li_locs:
                params = urllib.parse.urlencode({
                    "keywords":  f"{company} {kw}",
                    "location":  loc,
                    "f_TPR":     "r2592000",   # past 30 days
                    "start":     0,
                })
                url = (
                    "https://www.linkedin.com/jobs-guest/jobs/api/"
                    f"seeMoreJobPostings/search?{params}"
                )
                req = urllib.request.Request(url, headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Safari/537.36"
                    ),
                    "Accept":          "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer":         "https://www.linkedin.com/",
                })
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")

                # LinkedIn HTML structure (as of 2025):
                #   <h3 class="base-search-card__title">\n  Title text\n  </h3>
                #   <a class="hidden-nested-link" ...>\n  Company Name\n  </a>
                #   <span class="job-search-card__location">Location</span>
                titles    = re.findall(
                    r'class="base-search-card__title"[^>]*>\s*\n\s*([^\n<]+)', html)
                companies_found = re.findall(
                    r'class="hidden-nested-link"[^>]*>\s*\n\s*([^\n<]+)', html)
                locs      = re.findall(
                    r'class="job-search-card__location"[^>]*>\s*(.*?)\s*</span>', html, re.DOTALL)
                urls      = re.findall(
                    r'href="(https://[^"]+/jobs/view/[^"?]+)', html)

                for i, title in enumerate(titles):
                    co = companies_found[i].strip() if i < len(companies_found) else ""
                    if company.lower() not in co.lower():
                        continue
                    l = locs[i].strip() if i < len(locs) else loc
                    u = urls[i]         if i < len(urls)  else ""
                    found.append(_job_entry(company, title.strip(), l, u))

                time.sleep(0.6)

        log(f"{company}: {len(found)} jobs from LinkedIn")
    except Exception as e:
        log(f"{company}: LinkedIn search error — {e}")
    return found


# ── Main entry point ──────────────────────────────────────────────────────────

def run_scan(progress_callback=None):
    """
    Scan all configured companies and merge results into jobs.json.
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

    # Dispatch table for companies with confirmed / well-known job boards
    KNOWN = {
        "Anthropic":  lambda: _greenhouse("Anthropic",  "anthropic", keywords, locations, log) or [],
        "Mistral":    lambda: _lever("Mistral",    "mistral",   keywords, locations, log) or [],
        "Mistral AI": lambda: _lever("Mistral AI", "mistral",   keywords, locations, log) or [],
        "Google":     lambda: _google(keywords, locations, log),
        "Microsoft":  lambda: _microsoft(keywords, locations, log),
        "NVIDIA":     lambda: _nvidia(keywords, locations, log),
        "AWS":        lambda: _amazon(keywords, locations, log),
        "Amazon":     lambda: _amazon(keywords, locations, log),
        "Databricks": lambda: _greenhouse("Databricks", "databricks", keywords, locations, log) or [],
        "Datarobot":  lambda: _greenhouse("Datarobot",  "datarobot",  keywords, locations, log) or [],
        "Nebius":     lambda: _ashby("Nebius", "nebius", keywords, locations, log) or [],
    }

    for company in companies:
        log(f"Searching {company}…")
        fn = KNOWN.get(company)
        if fn:
            all_found += fn()
        else:
            # Auto-detect platform (OpenAI, Cohere, Poolside, Nebius AI, etc.)
            all_found += _try_platforms(company, keywords, locations, log)

    # ── Merge with existing jobs ──────────────────────────────────────────────
    if os.path.exists(JOBS_FILE):
        with open(JOBS_FILE) as f:
            existing = json.load(f)
    else:
        existing = {"last_scan": None, "jobs": []}

    existing_ids = {j["id"] for j in existing.get("jobs", [])}
    new_jobs     = [j for j in all_found if j["id"] not in existing_ids]
    merged       = existing.get("jobs", []) + new_jobs

    seen, deduped = set(), []
    for j in reversed(merged):
        if j["id"] not in seen:
            seen.add(j["id"])
            deduped.append(j)
    deduped.reverse()

    result = {
        "last_scan": datetime.now().isoformat(timespec="seconds"),
        "jobs":      deduped,
    }

    with open(JOBS_FILE, "w") as f:
        json.dump(result, f, indent=2)

    log(f"Done — {len(new_jobs)} new jobs added, {len(deduped)} total in database")
    return len(new_jobs), len(deduped)


if __name__ == "__main__":
    run_scan(print)
