from timeline.target_api import TargetClient, TargetConfig


class Resp:
    def __init__(self, code, body, headers=None):
        self.status_code, self._body, self.headers, self.text = code, body, headers or {}, str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data, timeout):
        return Resp(200, {"access_token": "tok", "expires_in": 86400})

    def get(self, url, headers, params, timeout):
        self.calls.append((url, params, headers))
        if url.endswith("/target/activities"):
            off = params["offset"]
            acts = [{"id": i} for i in range(off, min(off + 2, 3))]
            return Resp(200, {"total": 3, "offset": off, "limit": 2, "activities": acts})
        return Resp(200, {"total": 1, "activityChangelogs": [{"modifiedAt": "2026-09-01T00:00:00Z"}]})


def test_paging_and_headers():
    s = FakeSession()
    c = TargetClient(TargetConfig("tm", "cid", "sec", page_size=2), session=s)
    assert [a["id"] for a in c.list_activities()] == [0, 1, 2]
    assert len(c.activity_history(5)) == 1
    url, _, headers = s.calls[-1]
    assert url == "https://mc.adobe.io/tm/target/activities/5/changelog"
    assert headers["Authorization"] == "Bearer tok" and headers["X-Api-Key"] == "cid"
