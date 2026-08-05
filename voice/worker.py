"""A detached Claude Agent SDK run that does real work in a real project.

The voice brain in brain.py is deliberately fast and short: six turns, one
to three sentences, and a system prompt that tells it to decline anything
substantial. That is correct for conversation and useless for work.

This is the other half. A worker boots with its working directory set to a
project root, so that project's CLAUDE.md loads and it starts with the
stack, the commands, and the traps already in context. It gets no turn cap
worth speaking of and no instruction to be brief, because nobody is
listening to it talk.

Crucially it runs as its own asyncio task. main.py awaits brain.think()
inline, which is why a long answer freezes the microphone; a worker is
never awaited by the audio loop, so Chris can keep talking while it works.

Auth: reuses brain._subprocess_env(use_api_key=False), so the Max-plan
guarantee is exactly the one already audited. There is no second auth path
here to get wrong.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from brain import _subprocess_env, MODEL
from projects import Project

log = logging.getLogger("cletus-worker")

# Real work needs room. This is a runaway backstop, not a budget: a job that
# genuinely needs 200 turns is doing something wrong, but 6 (the voice cap)
# cannot finish a sentence of real work.
WORKER_MAX_TURNS = 200

# Appended to the Claude Code preset. The worker is NOT being listened to, so
# none of the voice constraints apply. What it does need is discipline about
# a machine nobody is watching.
WORKER_SYSTEM_APPEND = """
You are running as a background worker started by voice. Chris is not
watching this run and cannot answer questions mid-job.

Because nobody can approve anything while you work:

- NEVER push, deploy, merge to main, or delete anything outside the project
  you were pointed at. If the job seems to need one of those, do everything
  up to that line and stop there.
- Prefer creating a branch over committing to whatever is checked out.
- If the request is ambiguous, pick the reading that changes the least, do
  that, and say clearly in your final message what you assumed.
- If you cannot do the job safely without an answer, stop and say exactly
  what you need. A clean stop is a good outcome; a guess is not.

Your final message is read ALOUD to Chris through a speaker. Make the first
two sentences a plain-language summary of what you did or did not do. Put
any detail after that. No markdown, no file paths spelled out character by
character, no code blocks in the summary itself.
""".strip()


@dataclass
class Job:
    id: str
    project: Project
    request: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    ok: bool | None = None
    summary: str = ""
    tool_calls: int = 0
    turns: int = 0

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


async def run_job(job: Job, on_progress=None) -> Job:
    """Execute one job to completion. Never raises; failures land on the Job."""
    cwd: Path = job.project.path
    log.info("job %s starting  project=%s  cwd=%s  request=%r",
             job.id, job.project.key, cwd, job.request)

    options = ClaudeAgentOptions(
        model=MODEL,
        cwd=cwd,
        permission_mode="bypassPermissions",
        env=_subprocess_env(use_api_key=False),
        setting_sources=["user", "project", "local"],
        # The vault comes along so a job can read context and write notes,
        # but the working directory stays the project so its CLAUDE.md is
        # what auto-discovers.
        add_dirs=[str(Path.home() / "Documents" / "Brain")],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": WORKER_SYSTEM_APPEND,
        },
        max_turns=WORKER_MAX_TURNS,
    )

    parts: list[str] = []
    try:
        async for message in query(prompt=job.request, options=options):
            if isinstance(message, AssistantMessage):
                job.turns += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        job.tool_calls += 1
                        if on_progress is not None and job.tool_calls % 10 == 0:
                            try:
                                await on_progress(job)
                            except Exception:
                                pass
            elif isinstance(message, ResultMessage):
                job.turns = int(getattr(message, "num_turns", job.turns) or job.turns)

        job.summary = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
        job.ok = bool(job.summary)
        if not job.summary:
            job.summary = "The job finished but did not report anything back."
    except asyncio.CancelledError:
        job.ok = False
        job.summary = "That job was cancelled."
        raise
    except Exception as e:
        log.error("job %s failed: %s", job.id, e)
        job.ok = False
        job.summary = f"That job failed. {e}"
    finally:
        job.finished_at = time.time()
        log.info("job %s done  ok=%s  turns=%d  tools=%d  %.0fs",
                 job.id, job.ok, job.turns, job.tool_calls, job.elapsed)

    return job
