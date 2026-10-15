"""Read-only Jira Cloud client and the Target activity -> Jira ticket matching.

TM's Jira records a test's outcome as the ticket's status, e.g. "Winners",
"Losers" or "Inconclusive" (seen on EAOA-3006). Activity names in Target
usually end with the ticket key, e.g. "... - Authenticated (EAOA-3006)",
while the Jira summary is the same text without the key.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import requests

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")

# Jira status -> outcome shown in the app. Anything else is still in progress
# (or done without an outcome) and shows as "No outcome yet".
DEFAULT_OUTCOMES = {
    "winners": "Winner", "winner": "Winner",
    "losers": "Loser", "loser": "Loser",
    "inconclusive": "Inconclusive",
    "descoped": "Descoped",
}
NO_OUTCOME = "No outcome yet"
NOT_MATCHED = "No Jira ticket"


class JiraAPIError(RuntimeError):
    pass


@dataclass
class JiraConfig:
    site: str                      # e.g. "tescomobile.atlassian.net"
    email: str
    api_token: str
    # Which tickets to load for name matching. Keys found in activity names
    # are always looked up directly, even if outside this filter.
    jql: str = "project = EAOA AND labels = 120Feet"
    projects: list[str] = field(default_factory=lambda: ["EAOA"])
    timeout: int = 30


class JiraClient:
    FIELDS = ["summary", "status", "labels"]

    def __init__(self, cfg: JiraConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.auth = (cfg.email, cfg.api_token)
        self.base = "https://" + cfg.site.replace("https://", "").strip("/")

    def _search(self, jql: str) -> list[dict]:
        out, token = [], None
        while True:
            body = {"jql": jql, "fields": self.FIELDS, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            for attempt in range(4):
                resp = self.session.post(f"{self.base}/rest/api/3/search/jql", json=body,
                                         headers={"Accept": "application/json"}, timeout=self.cfg.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(min(float(resp.headers.get("Retry-After", 2 ** attempt)), 30))
                    continue
                break
            if resp.status_code != 200:
                raise JiraAPIError(f"Jira search failed ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            out.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def _simplify(self, issue: dict) -> dict:
        f = issue.get("fields", {})
        status = (f.get("status") or {})
        return {
            "key": issue.get("key"),
            "summary": f.get("summary") or "",
            "status": status.get("name") or "",
            "status_category": (status.get("statusCategory") or {}).get("key"),
            "url": f"{self.base}/browse/{issue.get('key')}",
        }

    def fetch(self, activity_names: list[str]) -> dict[str, dict]:
        """Tickets keyed by Jira key: everything matching the JQL filter, plus
        any key mentioned in an activity name."""
        issues = {i["key"]: self._simplify(i) for i in self._search(self.cfg.jql)}
        wanted = {k for n in activity_names for k in extract_keys(n, self.cfg.projects)} - set(issues)
        wanted = sorted(wanted)
        for i in range(0, len(wanted), 50):
            batch = wanted[i:i + 50]
            try:
                found = self._search(f"key in ({','.join(batch)})")
            except JiraAPIError:
                # One bad key (e.g. a deleted ticket) fails the whole batch; retry singly
                found = []
                for k in batch:
                    try:
                        found += self._search(f"key = {k}")
                    except JiraAPIError:
                        pass
            for iss in found:
                issues[iss["key"]] = self._simplify(iss)
        return issues


# ---------------------------------------------------------------- matching

def extract_keys(name: str, projects: list[str] | None = None) -> list[str]:
    keys = KEY_RE.findall(name or "")
    if projects:
        keys = [k for k in keys if k.split("-")[0] in projects]
    return keys


def _norm(text: str) -> str:
    text = KEY_RE.sub("", text or "")
    text = re.sub(r"[()\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def build_index(issues: dict[str, dict]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for key, iss in issues.items():
        index.setdefault(_norm(iss["summary"]), []).append(key)
    return index


def match_activity(name: str, issues: dict[str, dict], index: dict[str, list[str]],
                   projects: list[str] | None = None, outcomes: dict | None = None) -> dict:
    """Return the Jira match for one activity name."""
    outcomes = outcomes or DEFAULT_OUTCOMES
    method, key = None, None
    for k in extract_keys(name, projects):
        if k in issues:
            method, key = "Key in name", k
            break
    if key is None:
        hits = index.get(_norm(name), [])
        if len(hits) == 1:
            method, key = "Name", hits[0]
        elif len(hits) > 1:
            return {"jira_key": None, "jira_status": None, "jira_url": None, "outcome": NOT_MATCHED,
                    "jira_match": f"Several tickets share this name: {', '.join(hits[:3])}"}
    if key is None:
        return {"jira_key": None, "jira_status": None, "jira_url": None, "outcome": NOT_MATCHED,
                "jira_match": "No ticket with this key or name"}
    iss = issues[key]
    return {"jira_key": key, "jira_status": iss["status"], "jira_url": iss["url"],
            "outcome": outcomes.get(iss["status"].strip().lower(), NO_OUTCOME), "jira_match": method}
