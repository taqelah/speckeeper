# What each action costs in Gemini

Every Gemini call Spec Keeper makes logs its token counts (`speckeeper.usage` in Cloud Logging); the
message text is never logged. This prices the 32 calls logged on Cloud Run on Oct 15 and 16, 2026.

**Prices** (USD per million tokens; thinking is billed as output), from the
[Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing) on Oct 16, 2026:

| Model | Input | Output and thinking |
|---|---|---|
| Gemini 3.1 Pro Preview | $2.00 | $12.00 |
| Gemini 3.8 Flash | $0.75 (rises to $1.50 on Jan 1, 2027) | $3.75 (rises to $7.50 on Jan 1, 2027) |

## Cost per action

| Action | Model | Calls logged | Tokens (median) | Cost (median) | Most expensive |
|---|---|---|---|---|---|
| Capture a meeting (the kickoff notes) | 3.1 Pro | 6 | 5,700 | $0.051 | $0.068 |
| Review a thread change | 3.1 Pro | 3 | 3,700 | $0.032 | $0.040 |
| Thread watch: one check of a thread | 3.8 Flash, low thinking | 9 | 1,100 | $0.0010 | $0.0011 |
| Answer a "why?" question | 3.8 Flash, 2 to 5 calls | 4 questions | 4,100 | $0.006 | $0.010 |
| Approve, PRD, GitHub issues, CI result | none | – | 0 | $0 | – |

The "why?" row is 4 questions asked on the deployed agent on Oct 16 (13 model calls). The cheapest, a
list lookup, was $0.003; the dearest, which read two requirements' histories, was $0.010.

**One full demo** costs about **$0.09 (S$0.12):** a meeting capture, a few thread-watch checks, a
change review and a "why?" question. The S$383 trial credit covers about 3,000 of them.

## From BigQuery

Since Oct 16, every "why?" question also streams to BigQuery (`speckeeper_analytics.agent_events`,
see [analytics/queries.sql](../analytics/queries.sql)). The first 2 questions there:

| | Result |
|---|---|
| Cost per question | median $0.0024, max $0.0077, 2.5 Gemini calls on average |
| Whole question | 19 to 25 s |
| One Gemini call | p50 3.7 s; the slowest, 20.8 s, wrote the final answer with thinking |
| One tool call (Firestore lookup) | 40 to 73 ms |

Nearly all of a question's time is Gemini thinking; lookups take under 0.1 s. Lower thinking for the agent,
as for thread watch, is the next speed-up to try.

## What drives the cost

- **Thinking.** In meeting capture, thinking is 2,800 to 4,500 tokens, against 450 to 860 tokens of
  answer, so about three quarters of the cost. That is the price of the citation and judgement rules,
  and capture runs only when someone asks.
- **Low thinking on thread watch** cut a check from $0.022 to $0.001 (about 20 times cheaper) and from
  up to 42 s to about 3 s, with the same verdicts on every test case
  ([development results](development-results.md)). It is the call that runs most, once per quiet thread.
- **No Gemini call when nothing changed:** unchanged meeting notes are fingerprinted and skipped, and
  approvals, the PRD, GitHub issues and CI results never call Gemini.

## Limits

32 logged calls from a small demo, not production traffic. Prices are list prices for standard
pay-as-you-go use and change on Jan 1, 2027 for Flash.
