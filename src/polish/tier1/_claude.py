"""The edit engine that ships: Claude Agent SDK, inside the sandbox.

SPEC §3.2 chose an agent over codemods because "extract the 200 shared lines
from these five files" needs judgment, and §3.8 chose the Agent SDK specifically
so that credentials resolve the way Claude Code's do — an existing `ant auth
login` profile, or `ANTHROPIC_API_KEY`. Anyone already running Claude Code gets
zero-config Polish; everyone else exports a key.

This module is deliberately the thinnest thing that can satisfy `_agent.Agent`.
Everything that decides whether a change ships lives in `polish.tier1`, and none
of it is reachable from here. That is the shape SPEC §3.2's bargain requires:
the engine is a black box, so the box had better be small enough to see around.

## The tool allowlist, and why Bash is not on it

ADR 0006 established that the repository never supplies a program, and the
harness shape (ADR 0007) was admitted only because its program text is a
constant. Handing the agent `Bash` would undo both in one line: it would be a
general execution primitive inside the sandbox, chosen by a model, running
whatever a repository's `Makefile` suggested.

It would also break the thing the tier is *for*. If the agent can run the tests
itself it can form its own opinion about whether a change is good — and the
whole claim of this product is that the harness decided. So:

    plan     Read, Glob, Grep            — read-only, cannot edit at all
    edit     Read, Glob, Grep, Write, Edit — cannot execute, cannot reach git

The planning phase being read-only is worth stating on its own: the change queue
is produced by an agent that is *incapable* of having already edited something
by the time Polish tiers its proposal.

**What this does not claim.** The machine is still not isolated (SPEC §3.3), and
`Write` inside a worktree is `Write` — an agent that wanted to write outside
`cwd` is bounded by the SDK's own path handling, not by anything here. That is
the same honestly-stated weakness §3.3 already carries, not a new one.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ._agent import CATEGORIES, AgentError, Brief, Failure, Proposed

#: SPEC §3.8, and question #7's answer: one model for every stage until the
#: cost-per-verified-and-shipped-change experiment in #17 says otherwise. Per
#: stage routing is not a knob here, because #7 decided it is not a knob yet.
MODEL = "claude-opus-5"

#: Read-only. The planning phase cannot edit, so a proposal is always a proposal.
PLAN_TOOLS = ("Read", "Glob", "Grep")
#: Editing, with no execution and nothing that reaches git.
EDIT_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit")
#: Named rather than merely omitted. An allowlist that grows by accident grows
#: through the tools nobody wrote down as excluded.
DENIED = ("Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit")

#: A bound on the agent loop per call, so an unattended run cannot spend a night
#: in one conversation. Distinct from the run's token budget (#16), which bounds
#: the night; this bounds a single change.
PLAN_TURNS = 40
EDIT_TURNS = 30

#: How many changes one night proposes. A queue is a review surface before it is
#: a work list, and a hundred-commit branch is not one anybody reads.
MAX_QUEUE = 12

_FENCED = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

_PLAN_PROMPT = """\
You are proposing a queue of refactoring changes for a repository, to be applied
and verified tonight while nobody is watching. You are in planning mode: read the
code, propose the work, and edit nothing.

The repository root is your working directory. These are the files in scope:

{files}

Behaviour has already been recorded from a real run of this repository's test
suite. These symbols have a surviving characterization test, so a change to them
can be verified and can ship:

{eligible}

These symbols do NOT have one. A change that touches any of them will be refused
before it is applied, however good it is, so proposing work aimed at them wastes
the night:

{ineligible}

Propose up to {limit} changes. Each must be independent of the others: they are
applied one at a time, verified separately, and any one of them may be reverted
without affecting the rest. Prefer changes that remove real duplication or dead
code over cosmetic ones. Categories: {categories}.

Reply with a JSON array and nothing else. Each element:

  {{"category": "...",          one of the categories above
    "summary": "...",           one imperative line, becomes the commit subject
    "rationale": "...",         what moves where, and why behaviour is unchanged
    "paths": ["..."],           every file the change touches, repo-relative;
                                a path that does not exist yet is fine
    "symbols": ["..."],         dotted names the change touches
    "instructions": "..."}}     precise instructions for making the edit later

If there is no worthwhile work, reply with an empty array. That is a real answer
and a better one than a queue of changes nobody asked for.
"""

_EDIT_PROMPT = """\
Make exactly this one refactoring change, and nothing else.

Summary:    {summary}
Rationale:  {rationale}
Files:      {paths}

Instructions:
{instructions}

Rules:
- Edit only the files listed above. Touching any other file causes this change to
  be reverted in full, so it is worse than not making it.
- Preserve behaviour exactly. The change will be checked against characterization
  tests recorded from a real run, the repository's own suite, and a type check.
- Do not add tests, comments about this change, or unrelated tidying.
"""

_REPAIR_PROMPT = """\
The change you just made failed verification. You have exactly one attempt to fix
it. If it fails again the change is reverted in full and the night moves on, so
do not attempt a broader rewrite.

The change was:  {summary}
Files:           {paths}

What went wrong:

{detail}

Fix the existing edit in place. If the failure shows that the change cannot
preserve behaviour, revert your own edit back to what it was rather than leaving
something half-done — a clean revert is a better outcome than a broken change.
"""


@dataclass
class ClaudeAgent:
    """`_agent.Agent`, backed by the Claude Agent SDK.

    Each call is its own session, and that is a choice rather than a limitation.
    A repair with the failing edit's conversation still attached inherits the
    reasoning that produced the failure; a fresh session gets the code as it now
    is plus the verbatim failure, which is what a second engineer would get.
    """

    worktree: Path
    model: str = MODEL
    #: Every prompt sent, for the run log. The probe logs every command it ran
    #: on the same principle: a claim the user cannot check is one they have to
    #: take on faith.
    sessions: list[str] = field(default_factory=list)

    def propose(self, brief: Brief) -> Sequence[Proposed]:
        reply = self._ask(
            _PLAN_PROMPT.format(
                files=_bullets(brief.files),
                eligible=_bullets(brief.eligible) or "  (none)",
                ineligible=_bullets(brief.ineligible) or "  (none)",
                limit=MAX_QUEUE,
                categories=", ".join(CATEGORIES),
            ),
            tools=PLAN_TOOLS,
            turns=PLAN_TURNS,
            editing=False,
        )
        return _parse(reply)[:MAX_QUEUE]

    def apply(self, change: Proposed) -> None:
        self._ask(
            _EDIT_PROMPT.format(
                summary=change.summary,
                rationale=change.rationale,
                paths=", ".join(change.paths),
                instructions=change.instructions or change.rationale,
            ),
            tools=EDIT_TOOLS,
            turns=EDIT_TURNS,
            editing=True,
        )

    def repair(self, failure: Failure) -> None:
        self._ask(
            _REPAIR_PROMPT.format(
                summary=failure.change.summary,
                paths=", ".join(failure.change.paths),
                detail=failure.detail,
            ),
            tools=EDIT_TOOLS,
            turns=EDIT_TURNS,
            editing=True,
        )

    def _ask(
        self, prompt: str, *, tools: Sequence[str], turns: int, editing: bool
    ) -> str:
        """One session, run to completion. Returns the agent's final message.

        Synchronous by construction: the SDK is async and the rest of Polish is
        not, and the boundary is here rather than smeared through the loop. One
        `asyncio.run` per call is exactly right for a session that does not
        outlive the call.
        """
        self.sessions.append(prompt.splitlines()[0])
        try:
            return asyncio.run(
                self._session(prompt, tools=tools, turns=turns, editing=editing)
            )
        except AgentError:
            raise
        except Exception as failure:  # noqa: BLE001 - the SDK's errors are its own
            # Deliberately broad. The SDK raises transport errors, auth errors,
            # process errors and its own exception hierarchy, and this layer's
            # whole contract is that none of them end a night — they end one
            # change. Narrowing this to the SDK's classes would couple Polish to
            # a taxonomy it does not own, and let a new one abort a run.
            raise AgentError(f"the Claude Agent SDK failed: {failure}") from failure

    async def _session(
        self, prompt: str, *, tools: Sequence[str], turns: int, editing: bool
    ) -> str:
        try:
            from claude_agent_sdk import (  # type: ignore[import-not-found]
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as missing:
            raise AgentError(
                "Tier 1 edits need the Claude Agent SDK, which is not "
                "installed:\n\n  pip install claude-agent-sdk\n\n"
                f"({missing})"
            ) from missing

        options = ClaudeAgentOptions(
            cwd=str(self.worktree),
            model=self.model,
            allowed_tools=list(tools),
            disallowed_tools=list(DENIED),
            # Unattended by definition (SPEC §1.2: no chat, no babysitting), so
            # there is nobody to answer a permission prompt. The allowlist above
            # is what makes that safe to say; the mode only stops the run
            # blocking on a question at 3am.
            permission_mode="acceptEdits" if editing else "dontAsk",
            max_turns=turns,
        )

        said: list[str] = []
        result: object | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                said += [
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                ]
            elif isinstance(message, ResultMessage):
                result = message

        # Read through `getattr` rather than attribute access, and treat a
        # missing field as "no objection". The SDK's result type carries
        # eighteen fields today and is not Polish's to pin — a version that
        # renames one must cost a change, not every night.
        if getattr(result, "is_error", False):
            detail = getattr(result, "result", "") or getattr(result, "subtype", "")
            raise AgentError(f"the agent reported an error: {detail or 'no detail'}")

        reason = getattr(result, "terminal_reason", None)
        if reason is not None and reason != "success":
            # `maxTurnsReached` lands here, which is the common one: the change
            # is half-made, and half-made is exactly what the drift check and
            # the verifier downstream are for. Refusing it as an `AgentError`
            # is what gets the tree put back.
            raise AgentError(f"the agent stopped early: {reason}")
        return getattr(result, "result", None) or "\n".join(said)


def _parse(reply: str) -> list[Proposed]:
    """Read the change queue out of the agent's final message.

    A reply that is not a queue at all is an `AgentError`, which the loop turns
    into a declined tier — a night that could not be planned is worth saying out
    loud. But **one unusable entry only costs that entry**, on exactly the same
    principle as the loop it feeds: a queue of nine good changes and one with a
    category nobody recognises is nine changes, not a wasted night.
    """
    payload = _json(reply)
    if not isinstance(payload, list):
        raise AgentError("the agent's change queue was not a JSON array.")

    queue: list[Proposed] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            queue.append(
                Proposed(
                    category=str(entry.get("category", "")),
                    summary=str(entry.get("summary", "")).strip(),
                    rationale=str(entry.get("rationale", "")).strip(),
                    paths=tuple(_strings(entry.get("paths"))),
                    symbols=tuple(_strings(entry.get("symbols"))),
                    instructions=str(entry.get("instructions", "")).strip(),
                )
            )
        except AgentError:
            # `Proposed` validates its own category and path set. An entry that
            # fails is one the agent malformed, and dropping it is the smallest
            # thing that can be done about it.
            continue
    return queue


def _json(reply: str) -> object:
    for candidate in (*(match.group(1) for match in _FENCED.finditer(reply)), reply):
        try:
            return json.loads(candidate.strip())
        except ValueError:
            continue
    raise AgentError("the agent did not reply with a JSON change queue.")


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"  {item}" for item in items)
