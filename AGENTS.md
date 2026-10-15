# AGENTS.md

How to work on **Spec Keeper**. The user-facing overview is [README.md](README.md); this file is for
changing the code, whether you are a person or a coding agent.

## What this is

Spec Keeper is a Slack agent that makes sure what a team agreed on is what actually shipped. It
captures requirements from meeting notes and Slack threads (each cited to where it was agreed),
gets a person to approve them, writes them to the PRD and GitHub issues with keyed acceptance
criteria (AC-7-1), checks CI results per criterion, and catches later messages that change an
approved requirement. Built for the AI Builder Cup 2026 (Future of Work & Enterprise Productivity)
on Gemini (Vertex AI), the Agent Development Kit, Cloud Run and Firestore.

The demo product it watches is [taqelah/checkout-app](https://github.com/taqelah/checkout-app).

## Rules

1. **Nothing is written without a person.** Capture only proposes. The PRD, GitHub issues and
   requirement versions change only on Approve. Never add a path that writes on its own.
2. **No citation, no requirement.** Every requirement must cite a message or meeting passage that
   was really in the input. `extract.py` drops anything Gemini cites that isn't there; keep that.
3. **A change is a new version of the same requirement**, never a second requirement. REQ-7 v1
   becomes REQ-7 v2; criteria keep their keys; a criterion whose text changed goes stale if a test
   had proved the old text.
4. **Tests never touch the network.** No real Slack, Gemini, Google or GitHub calls in `tests/`.
   Use the fakes (see Testing).
5. **Secrets stay out of git.** `.env` and `.data/` are ignored; keep them that way. Never print a
   token or secret in logs, tests or commit messages.
6. **Every Slack request is verified** (signing secret) and `/ci` needs `X-SpecKeeper-Secret`. Don't
   add endpoints without the same protection. The API listing (`/docs`) stays off.
7. **Commits:** plain messages describing the change. No tool attribution lines (`Co-Authored-By`,
   "Generated with …").

## Commands

```sh
uv sync                                   # install (Python 3.12, managed by uv)
uv run pytest                             # all tests, ~1 s, no network
uv run pytest tests/test_e2e.py -k N_0    # a subset
uv run --env-file .env uvicorn --factory speckeeper.main:app --port 8080 --reload --reload-exclude ".data/*"
cloudflared tunnel --url http://localhost:8080        # public URL for Slack and GitHub while local
uv run --env-file .env adk web agents                 # ADK web UI for the question agent
uv run --env-file .env --group eval python -m evals.qa.run   # ADK eval of the question agent (real Gemini, ~10 min)
uv run --env-file .env python -m speckeeper.backfill  # issues for requirements approved before GitHub was set up (server stopped)
GCP_PROJECT=... GITHUB_REPO=... PRD_DOC_ID=... ./deploy.sh   # Cloud Run
```

## Code map

| Path | Role |
|---|---|
| `src/speckeeper/main.py` | FastAPI app: `/slack/events`, `/ci`, `/health`; builds the service from settings |
| `src/speckeeper/slack_app.py` | Slack Bolt handlers: mentions, card buttons, channel messages |
| `src/speckeeper/service.py` | What Spec Keeper does, independent of Slack: capture, approve, change, dismiss, CI, PRD versions |
| `src/speckeeper/extract.py` | Capture: one schema-validated Gemini call; uncited items dropped; changes to approved requirements marked |
| `src/speckeeper/agent.py` | ADK agent that answers questions (`why …?`) with tools over the store; Flash with Pro fallback |
| `src/speckeeper/notes.py` | Meeting notes in: Drive API as the service account, or a doc's shared-link export |
| `src/speckeeper/ci.py` | JUnit parsing, AC-key mapping, the stale rule, the Slack summary |
| `src/speckeeper/cards.py` | Block Kit: approval, baseline, change and decided cards |
| `src/speckeeper/integrations.py` | Google Docs PRD (append only) and GitHub issues |
| `src/speckeeper/store.py` | `MemoryStore` (tests), `FileStore` (local), `FirestoreStore` (Cloud Run) |
| `src/speckeeper/models.py` | Source, AcceptanceCriterion, Requirement, Proposal |
| `src/speckeeper/config.py` | Settings from the environment |
| `agents/speckeeper_qa/` | Entry point for `adk web` |
| `evals/notes/` | Meeting notes fixtures |
| `evals/qa/` | ADK eval of the question agent: `fixture.py` (a fixed project), `cases.py` (questions, expected tool calls, reference answers), `run.py` (AgentEvaluator; writes `report.json`) |

## Data model

- **Requirement** `REQ-n`: title, `version`, status (`proposed`, `approved`, `dismissed`,
  `superseded`), acceptance criteria, sources, `issue_number`.
- **AcceptanceCriterion** `AC-n-i`: Gherkin text, `version`, `ci_status` (`none`, `passing`,
  `failing`, `stale`), the last test that reported on it.
- **Source**: a Slack message (`kind="slack"`, user, ts, permalink, label "Name, Oct 15 16:02") or a
  meeting passage (`kind="meeting"`, label "Checkout v2 kickoff, 00:06:45", link to the doc).
- **Proposal**: what a card asks to approve. `kind` is `new` or `change` (`target` = the requirement
  it changes); `group_id` ties a meeting's proposals together; `previous` keeps the requirement as it
  was before an approved change; `decided_by` and `decided_at` record the approval.
- **Meta**: `prd_version` (v1.0.0 = the first meeting's baseline, every later approval a minor
  version), `baseline_group`, `doc:<id>` (notes already captured).

## Main flows

1. **Meeting:** `@Spec Keeper capture <notes link>` → read the doc → extract → one baseline card →
   Approve all (or one by one) → REQ keys, issues, PRD sections, PRD v1.0.0. The notes are
   fingerprinted: capturing the same text again calls no model (and re-posts a card for anything
   still waiting); capturing edited notes reads them against the approved requirements and returns
   only what's new (approval cards) or changed (change cards).
2. **Thread:** `@Spec Keeper capture this` → read the thread with names and permalinks → extract
   with the approved requirements in the prompt → approval card for new items, change card for
   changes → Approve → new requirement, or the next version of the existing one.
3. **CI:** GitHub Actions posts JUnit to `/ci` → each test maps to an AC by the key at the start of
   its name (`AC-7-1@v1 …`) → passing, failing or stale → Slack message and issue comment.
4. **Questions:** any mention phrased as a question → the ADK agent → tools read the store →
   answer with sources.

Slack acks every request within 3 seconds; handlers do the slow work after. On Cloud Run this
needs CPU always allocated (`deploy.sh` sets `--no-cpu-throttling`, min 1 instance).

## Testing

- `pytest`, fakes only. `tests/test_core.py` (service), `test_meeting.py` (meetings, versions,
  changes), `test_agent.py` (ADK tools, fallback), `test_app.py` (HTTP basics), `test_e2e.py`
  (whole app).
- **End-to-end tests** send Slack-signed requests through the real FastAPI and Bolt app. Slack is a
  local fake HTTP server (`FakeSlack`) that answers the Web API and records every call; Bolt builds
  its own client per request, so injecting a client object doesn't work, a fake server does.
  `create_app(..., slack_client=..., fetch_doc=..., wait=True)` makes handlers finish before the
  response so tests can assert on what was posted.
- Test ids (`E2E-`, `N-`, `C-`) match the test plan in the team's build plan doc. Add a negative and
  a corner case with every feature.
- **ADK eval** (`evals/qa/`) asks the question agent 13 questions against a fixed in-memory project
  and scores tool trajectory (IN_ORDER), answer match and hallucinations with a Gemini judge. It uses
  real Gemini, so it is run by hand, not in CI; `tests/test_evals.py` checks without Gemini that every
  case's expected tools return the facts its reference answer states. Change a case and the fixture together.
- What good tests here assert: what reaches Gemini (citations, approved requirements, no extra
  text), what is written and how often (issues, PRD, versions), and what the user sees in Slack.

## Configuration

`.env` (see `.env.example`): `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `GCP_PROJECT`,
`GCP_LOCATION` (asia-southeast1, for Cloud Run and Firestore), `GEMINI_LOCATION` (global),
`GEMINI_PRO_MODEL`, `GEMINI_FLASH_MODEL`, `PRD_DOC_ID`, `GITHUB_TOKEN`, `GITHUB_REPO`, `CI_SECRET`,
`SPECKEEPER_STORE` (`memory`, `file`, `firestore`), `FIRST_REQ` (7 for the demo).

## Things that bit us

- **Gemini models by region:** asia-southeast1 only offers old models. Use the `global` endpoint
  (`GEMINI_LOCATION`) for Gemini 3.x; Cloud Run and Firestore stay in asia-southeast1.
- **Preview model quotas are small.** `gemini-3.1-pro-preview` returns 429 under light load; capture
  and questions retry once on the other model. Raise the quota before the demo.
- **Google blocks the gcloud login from asking for Docs or Drive scopes.** Read and write Google docs
  as the `speckeeper` service account (impersonation locally, the service's identity on Cloud Run).
- **Slack private channels** need `groups:history`, `groups:read` and the `message.groups` event,
  besides the public-channel scopes.
- **The quick tunnel's URL changes on every restart**; update both Slack Request URLs and the
  `SPECKEEPER_URL` secret in checkout-app. Cloud Run removes this.
- **Loading `.env` into a shell sets `GITHUB_TOKEN`, which `gh` then uses** instead of your login.
  Run `gh` with `env -u GITHUB_TOKEN`.
- **The server keeps its own copy of the file store**; stop it before scripts that write to
  `.data/store.json`.
- **Write tests from the approved criteria, not from memory.** Gemini words the criteria; a test
  named `AC-7-1` that checks something else is a false green.
