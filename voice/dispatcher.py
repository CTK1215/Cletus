"""Decides whether a spoken sentence is a work order, and runs it if so.

The problem this solves: brain.think() is awaited inline in main.py, so any
long answer freezes the microphone, the HUD, and the wake word for its whole
duration. A twenty-minute job would make Cletus unusable for twenty minutes.

So work is split off. Conversation keeps going through the fast brain; a work
order becomes a background task that main.py never awaits.

Detection is deliberately conservative. Two things must both be true:

  1. Chris names a project out loud, and
  2. the sentence contains a verb that means "do something", not "tell me
     something"

or he uses an explicit dispatch phrase ("work on", "start a job").

Guessing which repo he meant and then editing files there is by far the worst
failure mode available to this feature, so when nothing matches, nothing is
dispatched and the sentence goes to the normal conversational brain. A missed
dispatch costs one repeated sentence. A wrong one edits the wrong codebase.
"""

import asyncio
import logging
import re
import time

from projects import Project, resolve, exists, spoken_list
from worker import Job, run_job

log = logging.getLogger("cletus-dispatch")

# Verbs that mean "act", not "answer". "What's in the admin?" must stay a
# conversation; "fix the admin build" must not.
ACTION_VERBS = (
    "build", "rebuild", "draft", "write", "rewrite", "fix", "run", "add",
    "create", "make", "update", "refactor", "clean up", "generate", "set up",
    "wire up", "implement", "migrate", "rename", "audit", "review", "check",
    "investigate", "look into", "figure out", "test", "commit", "scaffold",
)

# Always dispatch, whatever else is in the sentence. The escape hatch for when
# detection guesses wrong and Chris wants to be explicit.
EXPLICIT_PREFIXES = (
    "work on", "start a job", "start a job on", "kick off", "go build",
    "take on", "job on",
)

# Phrases that mean "tell me", even when an action verb is present. "Check
# whether the tests pass" is a question; "run the tests" is a job.
QUESTION_LEADS = (
    "what", "when", "who", "where", "why", "how", "is ", "are ", "do you",
    "did you", "can you tell", "remind me", "tell me",
)


class Dispatcher:
    """Owns the running jobs and the decision to start one."""

    def __init__(self, on_event=None, speak=None):
        # on_event(dict) -> broadcast to the HUD
        # speak(str) -> say something out loud
        self._on_event = on_event
        self._speak = speak
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._seq = 0

    # -- introspection -------------------------------------------------

    @property
    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.finished_at is None]

    def status_line(self) -> str:
        """Spoken answer to 'what are you working on?'"""
        live = self.running
        if not live:
            return "Nothing running right now."
        if len(live) == 1:
            j = live[0]
            return f"Still working on {j.project.spoken}, about {int(j.elapsed // 60)} minutes in."
        return f"{len(live)} jobs running: " + ", ".join(j.project.spoken for j in live)

    # -- detection -----------------------------------------------------

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        low = text.strip().lower()
        return low.endswith("?") or low.startswith(QUESTION_LEADS)

    def classify(self, text: str) -> tuple[Project | None, bool, str | None]:
        """Returns (project, should_dispatch, reason_not_to).

        reason_not_to is a spoken sentence when Chris clearly wanted a job but
        it cannot be started, so he hears why instead of silence.
        """
        low = text.strip().lower()
        explicit = any(p in low for p in EXPLICIT_PREFIXES)
        project = resolve(text)

        if explicit and project is None:
            return None, False, (
                f"I can work on {spoken_list()}. Which one?"
            )

        if project is None:
            return None, False, None

        if not exists(project):
            return project, False, (
                f"I know {project.spoken} but I can't find its folder on this machine."
            )

        if explicit:
            return project, True, None

        # Not explicit: needs an action verb and must not read as a question.
        if self._looks_like_question(text):
            return project, False, None

        has_verb = any(re.search(rf"\b{re.escape(v)}\b", low) for v in ACTION_VERBS)
        if not has_verb:
            return project, False, None

        return project, True, None

    # -- dispatch ------------------------------------------------------

    async def maybe_dispatch(self, text: str) -> bool:
        """Start a job if this is a work order. True means handled; main.py
        should NOT send this to the conversational brain."""
        project, go, reason = self.classify(text)

        if reason is not None and self._speak is not None:
            await self._speak(reason)
            return True

        if not go or project is None:
            return False

        self._seq += 1
        job = Job(id=f"job{self._seq}", project=project, request=text)
        self._jobs[job.id] = job

        await self._emit({
            "event": "job-started",
            "id": job.id,
            "project": project.key,
            "projectSpoken": project.spoken,
            "request": text,
        })

        if self._speak is not None:
            await self._speak(f"On it. Working on {project.spoken}. I'll tell you when it's done.")

        self._tasks[job.id] = asyncio.create_task(self._run(job))
        return True

    async def _run(self, job: Job) -> None:
        async def on_progress(j: Job) -> None:
            await self._emit({
                "event": "job-progress",
                "id": j.id,
                "toolCalls": j.tool_calls,
                "elapsed": int(j.elapsed),
            })

        try:
            await run_job(job, on_progress=on_progress)
        finally:
            self._tasks.pop(job.id, None)

        await self._emit({
            "event": "job-done",
            "id": job.id,
            "project": job.project.key,
            "ok": bool(job.ok),
            "elapsed": int(job.elapsed),
            "summary": job.summary,
        })

        if self._speak is not None:
            mins = int(job.elapsed // 60)
            when = f" That took about {mins} minutes." if mins >= 2 else ""
            lead = "Done with" if job.ok else "I hit a problem on"
            await self._speak(f"{lead} {job.project.spoken}.{when} {job.summary}")

    async def _emit(self, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(payload)
        except Exception as e:
            log.warning("job event broadcast failed: %s", e)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
