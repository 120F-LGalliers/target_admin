from timeline.jira_api import build_index, match_activity, extract_keys, JiraClient, JiraConfig

ISSUES = {
    "EAOA-3006": {"key": "EAOA-3006", "summary": "Checkout Steps Display & Progression - [Checkout Workshop] - Authenticated",
                  "status": "Winners", "url": "u1"},
    "EAOA-100": {"key": "EAOA-100", "summary": "T_120feet_MVP Header", "status": "Run", "url": "u2"},
    "EAOA-200": {"key": "EAOA-200", "summary": "Dupe", "status": "Losers", "url": "u3"},
    "EAOA-201": {"key": "EAOA-201", "summary": "Dupe", "status": "Losers", "url": "u4"},
}
IDX = build_index(ISSUES)


def test_key_in_activity_name():
    m = match_activity("Checkout Steps Display & Progression - [Checkout Workshop] - Authenticated (EAOA-3006)",
                       ISSUES, IDX, ["EAOA"])
    assert m["jira_key"] == "EAOA-3006" and m["outcome"] == "Winner" and m["jira_match"] == "Key in name"


def test_name_fallback_and_in_progress():
    m = match_activity("T_120feet_MVP Header", ISSUES, IDX, ["EAOA"])
    assert m["jira_key"] == "EAOA-100" and m["outcome"] == "No outcome yet"


def test_ambiguous_and_missing():
    assert match_activity("Dupe", ISSUES, IDX)["jira_key"] is None
    assert match_activity("Nothing like it", ISSUES, IDX)["outcome"] == "No Jira ticket"


def test_extract_keys_respects_projects():
    assert extract_keys("[120f] TMOP-21-MA-Help (EAOA-12)", ["EAOA"]) == ["EAOA-12"]


class _Resp:
    def __init__(self, body): self.status_code, self._b, self.headers, self.text = 200, body, {}, ""
    def json(self): return self._b


class _Sess:
    def __init__(self): self.calls = []; self.auth = None
    def post(self, url, json, headers, timeout):
        self.calls.append(json)
        if "key in" in json["jql"]:
            return _Resp({"issues": [{"key": "EAOA-9", "fields": {"summary": "x", "status": {"name": "Losers"}}}]})
        if "nextPageToken" not in json:
            return _Resp({"issues": [{"key": "EAOA-1", "fields": {"summary": "a", "status": {"name": "Run"}}}],
                          "nextPageToken": "t"})
        return _Resp({"issues": [{"key": "EAOA-2", "fields": {"summary": "b", "status": {"name": "Winners"}}}]})


def test_client_pages_and_fetches_named_keys():
    c = JiraClient(JiraConfig("x.atlassian.net", "e", "t"), session=_Sess())
    issues = c.fetch(["Test (EAOA-9)", "Other (EAOA-1)"])
    assert set(issues) == {"EAOA-1", "EAOA-2", "EAOA-9"}
    assert issues["EAOA-9"]["url"] == "https://x.atlassian.net/browse/EAOA-9"
