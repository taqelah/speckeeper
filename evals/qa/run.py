"""Run the ADK evaluation of the "why?" agent and write the results.

    uv run --group eval python -m evals.qa.run            # 2 runs per case
    uv run --group eval python -m evals.qa.run --runs 1

Uses real Gemini on Vertex AI (the agent on Flash, the judge on Flash), so it costs a little and is not
part of the unit tests. Writes evals/qa/results/results.csv (every case and metric) and
evals/qa/report.json (the summary the landing page and deck quote), and an ADK eval set file that
`adk eval` and the ADK web UI can open."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from google.adk.evaluation.agent_evaluator import AgentEvaluator
from google.adk.evaluation.eval_case import EvalCase, IntermediateData, Invocation
from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types

from .cases import CASES

HERE = Path(__file__).parent
JUDGE = os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.8-flash")

# What "good" means, per ADK metric:
# - tool_trajectory_avg_score: it called the tools the answer needs, in order (extra calls allowed)
# - final_response_match_v2: a Gemini judge says the answer agrees with the reference answer
# - hallucinations_v1: a Gemini judge checks every claim in the answer is backed by what the tools returned
CONFIG = EvalConfig.model_validate(
    {
        "criteria": {
            "tool_trajectory_avg_score": {"threshold": 0.8, "match_type": "IN_ORDER"},
            "final_response_match_v2": {"threshold": 0.7, "judge_model_options": {"judge_model": JUDGE, "num_samples": 3}},
            "hallucinations_v1": {"threshold": 0.8, "judge_model_options": {"judge_model": JUDGE, "num_samples": 1}},
        }
    }
)


def eval_set() -> EvalSet:
    cases = []
    for case_id, question, tools, reference in CASES:
        cases.append(
            EvalCase(
                eval_id=case_id,
                conversation=[
                    Invocation(
                        invocation_id=f"{case_id}-1",
                        user_content=types.Content(role="user", parts=[types.Part(text=question)]),
                        final_response=types.Content(role="model", parts=[types.Part(text=reference)]),
                        intermediate_data=IntermediateData(tool_uses=[types.FunctionCall(name=name, args=args) for name, args in tools]),
                    )
                ],
            )
        )
    return EvalSet(eval_set_id="speckeeper_qa", name="Spec Keeper why-agent", eval_cases=cases)


def summarise(csv_path: Path, runs: int, seconds: float) -> dict:
    rows = list(csv.DictReader(csv_path.open()))
    by_metric: dict[str, list[dict]] = {}
    for row in rows:
        by_metric.setdefault(row.get("metric_name", ""), []).append(row)
    metrics, measured = {}, {}
    for name, items in sorted(by_metric.items()):
        scores = [float(r["score"]) for r in items if r.get("score") not in ("", None, "None")]
        average = round(sum(scores) / len(scores), 3) if scores else None
        if name in CONFIG.criteria:
            passed = sum(1 for r in items if (r.get("eval_status") or "").upper().endswith("PASSED"))
            metrics[name] = {"average": average, "passed": passed, "total": len(items), "threshold": CONFIG.criteria[name].threshold}
        else:
            # ADK also records speed, tokens and call counts for every run: no pass mark, just numbers
            measured[name] = {"average": average, "runs": len(items)}
    return {
        "eval_set": "speckeeper_qa",
        "cases": len(CASES),
        "runs_per_case": runs,
        "agent_model": os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.8-flash"),
        "judge_model": JUDGE,
        "metrics": metrics,
        "measured": measured,
        "seconds": round(seconds),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def main(runs: int) -> int:
    if not os.environ.get("GCP_PROJECT"):
        raise SystemExit("Set GCP_PROJECT to the project whose Gemini quota the eval may use.")
    es = eval_set()
    (HERE / "speckeeper_qa.evalset.json").write_text(es.model_dump_json(indent=2, exclude_none=True, by_alias=True))
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    csv_path = results / "results.csv"
    csv_path.unlink(missing_ok=True)  # ADK appends to it; a report covers one run of this script
    started = time.monotonic()
    ok = True
    try:
        await AgentEvaluator.evaluate_eval_set(
            agent_module="evals.qa", eval_set=es, eval_config=CONFIG, num_runs=runs, print_detailed_results=False, output_file=str(csv_path)
        )
    except AssertionError as failures:
        ok = False
        (results / "failures.txt").write_text(str(failures))
    report = summarise(csv_path, runs, time.monotonic() - started)
    (HERE / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["metrics"], indent=2))
    print("all cases passed" if ok else f"some cases failed: see {results / 'failures.txt'}")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=2, help="times each case is asked")
    raise SystemExit(asyncio.run(main(parser.parse_args().runs)))
