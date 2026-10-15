"""Open GitHub issues for approved requirements that don't have one yet: for requirements approved
before GitHub was connected. Run with the server stopped (it keeps its own copy of the store):

    uv run --env-file .env python -m speckeeper.backfill
"""

from __future__ import annotations

from .config import Settings
from .integrations import GitHubIssues
from .store import FileStore


def main() -> None:
    s = Settings.from_env()
    if not (s.github_token and s.github_repo):
        raise SystemExit("Set GITHUB_TOKEN and GITHUB_REPO in .env first.")
    store = FileStore(".data/store.json", s.first_req)
    issues = GitHubIssues(s.github_token, s.github_repo)
    todo = [r for r in store.requirements() if r.status == "approved" and not r.issue_number]
    for req in sorted(todo, key=lambda r: int(r.key.split("-")[1])):
        req.issue_number = issues.create(req)
        store.save_requirement(req)
        print(f"{req.key} -> https://github.com/{s.github_repo}/issues/{req.issue_number}")
    if not todo:
        print("Every approved requirement already has an issue.")


if __name__ == "__main__":
    main()
