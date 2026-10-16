"""ADK evaluation of the "why?" agent against a fixed project (fixture.py). Run: uv run --group eval python -m evals.qa.run"""

from . import agent  # noqa: F401  (AgentEvaluator loads `evals.qa` and looks for `agent.root_agent`)
