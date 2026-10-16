# Capture and drift: results from development runs

The ADK eval in [`qa/`](qa/) measures the "why?" agent. This file records how capture and thread watch
did in the runs made while building them on Oct 14 to 16, 2026, all on real Gemini (3.1 Pro for
capture, 3.8 Flash for thread watch). These are development checks on our own small, hand-written
cases, not a benchmark. No script here reproduces them: production deliberately never logs message
text, so the counts come from the run outputs recorded during development, and from the Cloud Logging
audit trail for the Oct 16 run.

On Oct 16 capture and thread watch moved into ADK multi-agent pipelines (`src/speckeeper/pipelines.py`)
with the same prompts, schemas and checks. These runs were made before that, with direct Gemini calls.

## Capture: meeting notes to requirements

The input is the Checkout v2 kickoff notes ([`notes/`](notes/)). They contain:

- **5 testable decisions,** which should become requirements: the 25 MB upload limit, resumable
  uploads, the virus scan, the download audit log and one-step saved-card payment.
- **1 scope decision,** "guest checkout moves to v2.1". It isn't testable, so it should not become a
  requirement.
- **2 open questions,** which should not become requirements: image compression, and how long to
  keep the audit log.

| Run | Where | Extracted | Correct | Wrong |
|---|---|---|---|---|
| Oct 14 | Local | 5 | 5 | 0 |
| Oct 15, 12:43 SGT | Production | 6 | 5 | 1: guest checkout (prompt changed after this run) |
| Oct 15, after the prompt change (live test L-01) | Production | 5 | 5 | 0 |
| Oct 16 | Production, new project | 5 | 5 | 0 |
| **Total** | | **21** | **20** | **1** |

- **Precision:** 20 / 21 = **0.95**
- **Recall:** 20 / 20 = **1.00**
- **Open questions captured as requirements:** 0 of 8
- **Captures took** about 37 to 45 s.

**A change captured from a thread:** asked to review a thread that settles "cap uploads at 8 MB",
capture proposed REQ-7 v2 at 8 MB with both criteria rewritten, in 2 of 2 runs (11 to 14 s).

## Thread watch: spotting a change

Each case is a short Slack thread under an approved REQ-7 (25 MB, later 10 MB), labelled with the
expected verdict. Thread watch either suggests a change to an existing requirement, suggests a new
requirement, or stays silent.

| Run (Oct 15, SGT) | Setting | Change | New | Silent cases | Correct |
|---|---|---|---|---|---|
| 14:33 | Flash, default thinking | 1 / 1 | 1 / 1 | 3 / 3: harmless, idea only, injection | 5 / 5 |
| 14:41 | Flash, low thinking | 1 / 1 | 1 / 1 | 3 / 3: same | 5 / 5 |
| 14:47 | Flash, low thinking, settled-decision rule | 1 / 1 | 1 / 1 | 5 / 5: question, maybe, pushback, harmless, injection | 7 / 7 |
| 15:07 | Two wordings of "cap uploads at 6 MB" | 2 / 2 | – | – | 2 / 2 |
| 15:17 | After the injection fix | 1 / 1 | 1 / 1 | 5 / 5: same as 14:47 | 7 / 7 |
| **Total** | | **6 / 6** | **4 / 4** | **16 / 16** | **26 / 26** |

- **Changes caught:** 6 of 6. **New requirements caught:** 4 of 4. **False alarms:** 0 of 16.
- **Speed:** low thinking cut a decision from up to 42 s to about 2 to 4 s, with the same verdicts.

## Prompt injection

Three attack messages ran through capture and thread watch: a direct instruction, a fake "new rule
from the admin" setting the limit to 1 GB, and an instruction hidden in meeting notes.

| | Capture | Thread watch |
|---|---|---|
| Before the fix (Oct 15, 15:15 SGT) | 2 / 3 blocked: the fake rule became "Upload size limit is 1 GB" | 3 / 3 silent |
| After the fix (15:17) | **3 / 3 blocked** | **3 / 3 silent** |

The fix moved the rules into Gemini's system instruction and added a rule that a message giving
instructions or claiming authority is never a requirement.

## Limits

- **Small samples:** 4 meeting captures, and 26 thread-watch decisions from 7 distinct cases.
- **Cases written by us,** with the prompts changing between runs, so this is not a held-out test.
- **Not to be re-run in bulk against Vertex AI.** The first project's Vertex AI access was suspended on
  Oct 15 after repeated automated runs containing prompt-injection text. Run adversarial cases against
  fakes (see `tests/`).
