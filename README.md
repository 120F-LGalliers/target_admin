# TM Experiment Timeline

A Streamlit app that shows which Tesco Mobile Adobe Target tests **started**, **first went live** or **concluded** in a chosen date range.

## Run it locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

With no credentials it runs on made-up demo data, with a banner saying so.

## Connect to Adobe Target

1. In Adobe Developer Console, create a project, add the **Adobe Target** API and choose **OAuth Server-to-Server**.
2. Assign the project a product profile with **Observer** access to the Tesco Mobile workspace(s). The app only reads data.
3. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in `tenant`, `client_id` and `client_secret`. On the hosted app, put these in the host's secrets store, not in the repo.

## How it works

| File | What it does |
|---|---|
| `timeline/target_api.py` | Gets an OAuth token and calls `GET /target/activities` and `GET /target/activities/{id}/changelog` |
| `timeline/store.py` | Caches everything in `data/cache.json`. Refreshes when the cache is older than `refresh_hours`, or on "Refresh now". Only re-fetches change logs for activities whose `modifiedAt` changed |
| `timeline/lifecycle.py` | Works out the dates from each activity's change log and schedule |
| `app.py` | The page: date range, type filter, search, four tabs, CSV downloads |

### Date definitions

- **Started**: the most recent activation (moved to `approved`). For a relaunched test this is the relaunch.
- **First went live**: the first activation ever.
- **Concluded**: the last move from `approved` to `deactivated` or `archived`, or the scheduled end date passing.
- An activation with a future scheduled start counts from the scheduled start.
- With no state history, the app falls back to `startsAt` / `endsAt` and marks the row as "Schedule only".
- All dates are shown in UK time.

## Things to verify against TM's live account

1. **Change log contents.** The logic expects each change log entry to contain `modifiedAt` and the activity `state`, either at the top level or under `activityParameters`. Run one known test through and check the dates match.
2. **Revisions endpoint.** If the newer Revisions endpoint (`getRevisionHistory`) gives better data, set `history_path` in secrets and adjust `_dig()` in `lifecycle.py` if the field names differ.
3. **Scopes.** The default OAuth scopes match a standard Target S2S credential. Copy the exact list from the Developer Console project if login fails.
4. **Type codes.** Auto-Target and others are labelled from the API's `type` value. Check the labels look right on real data.

## Test outcomes from Jira

On each refresh the app also reads TM's Jira and matches every Target activity to its ticket:

1. **Key in the activity name.** Most names end with the ticket key, e.g. `... Authenticated (EAOA-3006)`.
2. **Name.** Otherwise, the activity name is matched to a ticket summary in the `jql` filter (case and spacing ignored).

The outcome is the ticket's status: `Winners` -> Winner, `Losers` -> Loser, `Inconclusive`, `Descoped`. Any other status shows as "No outcome yet"; no match shows as "No Jira ticket". Add a `[jira]` section to the secrets to turn this on.

The ticket's **End date** field (`customfield_12051` on EAOA) is shown as "Planned end (Jira)" in the tables, and on the calendar as a faint bar from today to that date for live tests. Set `end_date_field` under `[jira]` if TM ever use a different field.

## Tests

```bash
python -m pytest -q tests
```
