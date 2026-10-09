"""Find people to contact at a company.

- Brave Search API "X-ray" search (site:linkedin.com/in ...): names, titles, LinkedIn URLs.
  Never touches LinkedIn itself. Needs BRAVE_API_KEY.
- Apollo.io people search + match: titles and work emails. Needs APOLLO_API_KEY. Each email
  reveal uses an Apollo credit, so only the top contacts are revealed.
Either source is skipped when its key isn't set.
"""
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
APOLLO = "https://api.apollo.io/api/v1"

# Higher rank = contacted first: the hiring manager beats a teammate beats a recruiter.
RANKS = [
    (r"\b(engineering manager|head of engineering|director of engineering|vp,? engineering|cto|hiring manager)\b", 3),
    (r"\b(staff|senior|lead|principal)?\s*(software|backend|frontend|platform|data|ml|machine learning)\s+engineer\b", 2),
    (r"\b(technical recruiter|recruiter|talent|sourcer)\b", 1),
]


@dataclass
class Contact:
    name: str
    title: str
    company: str
    linkedin_url: str = ""
    email: str = ""
    source: str = ""
    rank: int = 0
    extra: dict = field(default_factory=dict, repr=False)


def rank_title(title: str) -> int:
    t = title.lower()
    return max((r for pattern, r in RANKS if re.search(pattern, t)), default=0)


def _clean_linkedin(url: str) -> str:
    """Normalise any linkedin.com/in/<slug> URL to https://www.linkedin.com/in/<slug>; '' if not a profile."""
    m = re.match(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#]+)", url)
    return f"https://www.linkedin.com/in/{m.group(1)}" if m else ""


def parse_brave(data: dict, company: str) -> list[Contact]:
    out = []
    for r in (data.get("web") or {}).get("results", []):
        url = _clean_linkedin(r.get("url", ""))
        if not url:
            continue
        # Titles look like "Jane Doe - Engineering Manager - Stripe | LinkedIn"
        parts = [p.strip() for p in re.split(r"\s[-–|]\s", r.get("title", "")) if p.strip()]
        parts = [p for p in parts if p.lower() != "linkedin"]
        if not parts:
            continue
        name, title = parts[0], (parts[1] if len(parts) > 1 else "")
        if company.lower() not in (r.get("title", "") + " " + r.get("description", "")).lower():
            continue  # probably a former employee or a namesake
        out.append(Contact(name, title, company, linkedin_url=url, source="brave", rank=rank_title(title)))
    return out


def search_brave(client: httpx.Client, company: str, role_hint: str, key: str) -> list[Contact]:
    roles = '("engineering manager" OR "hiring manager" OR recruiter OR "software engineer")'
    q = f'site:linkedin.com/in "{company}" {roles} {role_hint}'.strip()
    resp = client.get(BRAVE_URL, params={"q": q, "count": 20},
                      headers={"X-Subscription-Token": key, "Accept": "application/json"})
    resp.raise_for_status()
    return parse_brave(resp.json(), company)


def search_apollo(client: httpx.Client, company: str, key: str, reveal: int) -> list[Contact]:
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    orgs = client.post(f"{APOLLO}/mixed_companies/search", headers=headers,
                       json={"q_organization_name": company, "per_page": 1})
    orgs.raise_for_status()
    org_list = orgs.json().get("organizations") or orgs.json().get("accounts") or []
    if not org_list:
        return []
    org_id = org_list[0].get("id") or org_list[0].get("organization_id")
    people = client.post(f"{APOLLO}/mixed_people/search", headers=headers, json={
        "organization_ids": [org_id], "per_page": 25,
        "person_titles": ["engineering manager", "software engineer", "technical recruiter", "recruiter"],
    })
    people.raise_for_status()
    contacts = []
    for p in people.json().get("people", []):
        email = p.get("email") or ""
        if "not_unlocked" in email or "@domain.com" in email:
            email = ""
        contacts.append(Contact(p.get("name") or "", p.get("title") or "", company,
                                linkedin_url=p.get("linkedin_url") or "", email=email, source="apollo",
                                rank=rank_title(p.get("title") or ""), extra={"apollo_id": p.get("id")}))
    contacts.sort(key=lambda c: -c.rank)
    for c in contacts[:reveal]:
        if c.email or not c.extra.get("apollo_id"):
            continue
        m = client.post(f"{APOLLO}/people/match", headers=headers, json={"id": c.extra["apollo_id"]})
        if m.status_code == 200:
            email = (m.json().get("person") or {}).get("email") or ""
            if email and "not_unlocked" not in email:
                c.email = email
    return contacts


def merge(contacts: list[Contact]) -> list[Contact]:
    """One entry per person: Apollo's email joins Brave's profile when the LinkedIn URL or name matches."""
    by_key: dict[str, Contact] = {}
    for c in contacts:
        key = _clean_linkedin(c.linkedin_url).lower() or c.name.lower()
        if key in by_key:
            cur = by_key[key]
            cur.email = cur.email or c.email
            cur.title = cur.title or c.title
            cur.source = ",".join(sorted(set(cur.source.split(",")) | {c.source}))
        else:
            by_key[key] = c
    return list(by_key.values())


def find_contacts(company: str, role_hint: str = "", *, limit: int = 3,
                  client: httpx.Client | None = None) -> list[Contact]:
    """Best `limit` contacts, highest rank first; people with a rank of 0 are left out."""
    brave_key, apollo_key = os.environ.get("BRAVE_API_KEY"), os.environ.get("APOLLO_API_KEY")
    if not (brave_key or apollo_key):
        log.warning("neither BRAVE_API_KEY nor APOLLO_API_KEY is set; no contacts found")
        return []
    own = client is None
    client = client or httpx.Client(timeout=20)
    found = []
    try:
        if brave_key:
            try:
                found += search_brave(client, company, role_hint, brave_key)
            except httpx.HTTPError as exc:
                log.warning("brave search failed for %s: %s", company, exc)
        if apollo_key:
            try:
                found += search_apollo(client, company, apollo_key, reveal=limit)
            except httpx.HTTPError as exc:
                log.warning("apollo search failed for %s: %s", company, exc)
    finally:
        if own:
            client.close()
    ranked = sorted((c for c in merge(found) if c.rank > 0), key=lambda c: (-c.rank, not c.email))
    return ranked[:limit]
