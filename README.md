# Threadline

The Slack agent that makes sure what your team agreed on is what actually shipped.

Mention it in a thread (`@Threadline capture this`) and it turns what was agreed into a requirement
with Gherkin acceptance criteria, each citing the messages it came from. Nothing is written until a
person clicks Approve; then the requirement is added to the PRD in Google Docs and opened as a GitHub
issue with keyed criteria (AC-7-1, AC-7-2). CI results come back per criterion: tests named after an
AC key report passed, failed or stale in Slack and on the issue.

Built for the AI Builder Cup 2026 (Future of Work & Enterprise Productivity): Gemini on Vertex AI,
the Agent Development Kit, Cloud Run, Firestore.

## Run it locally

```sh
uv sync
cp .env.example .env            # fill in the Slack token and signing secret
uv run --env-file .env uvicorn --factory threadline.main:app --port 8080 --reload
cloudflared tunnel --url http://localhost:8080   # prints a public https URL
```

In the Slack app settings, set the Event Subscriptions and Interactivity request URLs to
`<tunnel URL>/slack/events`.

## Test

```sh
uv run pytest
```

Tests use fakes for Slack, Gemini, Google Docs and GitHub; nothing calls the network.

## Deploy

```sh
GCP_PROJECT=... GITHUB_REPO=org/checkout-app PRD_DOC_ID=... ./deploy.sh
```

## CI verification

Tests in the product repo start their name with the AC key and the AC version they were written for:
`AC-7-1@v1 rejects a 30 MB file`. The workflow posts the JUnit XML to `/ci` with the
`X-Threadline-Secret` header. A pass from a test tagged with an older version than the AC counts as
**stale**, not passing.

## Layout

| Path | What |
|---|---|
| `src/threadline/main.py` | FastAPI app: `/slack/events`, `/ci`, `/healthz` |
| `src/threadline/slack_app.py` | Mentions, card buttons, channel messages |
| `src/threadline/service.py` | Capture, approve, dismiss, CI, independent of Slack |
| `src/threadline/extract.py` | Gemini structured extraction; uncited items are dropped |
| `src/threadline/ci.py` | JUnit parsing, AC mapping, stale rule |
| `src/threadline/cards.py` | Block Kit approval card |
| `src/threadline/integrations.py` | Google Docs PRD and GitHub issues |
| `src/threadline/agent.py` | ADK root agent and its tools |
| `src/threadline/store.py` | In-memory and Firestore storage |
