"""The project graph: requirements, their acceptance criteria and the messages they came from."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CiStatus = Literal["none", "passing", "failing", "stale"]


class Source(BaseModel):
    """A Slack message an item was taken from. Every requirement cites at least one."""

    ts: str
    user: str
    text: str
    permalink: str = ""


class AcceptanceCriterion(BaseModel):
    key: str  # AC-7-1
    gherkin: str  # "Given … When … Then …", one step per line
    version: int = 1
    ci_status: CiStatus = "none"
    last_run: str = ""
    last_test: str = ""


class Requirement(BaseModel):
    key: str  # REQ-7, assigned on approval
    title: str
    version: int = 1
    status: Literal["proposed", "approved", "dismissed", "superseded"] = "proposed"
    acs: list[AcceptanceCriterion] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    issue_number: int | None = None

    @property
    def verified(self) -> bool:
        return bool(self.acs) and all(ac.ci_status == "passing" for ac in self.acs)


class Proposal(BaseModel):
    """What a card asks a person to approve. Nothing is written until it is approved."""

    id: str
    kind: Literal["new", "change"] = "new"
    requirement: Requirement
    channel: str
    thread_ts: str
    message_ts: str = ""  # the card's own message, to update it after a click
    status: Literal["pending", "approved", "dismissed"] = "pending"
    decided_by: str = ""
