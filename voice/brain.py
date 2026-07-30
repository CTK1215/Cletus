"""Cletus's brain, powered by Claude Agent SDK.

Dual-auth: tries Max plan (OAuth via stored Claude Code login) first, falls
back to ANTHROPIC_API_KEY on rate limit / auth failure. Full Claude Code
tool set is available: file I/O, Bash, Grep, MCP servers, skills. Working
directory is C:\\Users\\Christopher\\.cletus-voice so CLAUDE.md auto-discovers
up-tree and boots the Cletus identity.

Conversation memory: each exchange's session id is captured and the next
exchange resumes it, so back-to-back wakes are one continuous conversation.
The id persists to session.json (survives service restarts), expires after
SESSION_MAX_IDLE_S of quiet, and clears on a spoken reset phrase.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    RateLimitEvent,
)

log = logging.getLogger("cletus-brain")

MODEL = "claude-sonnet-4-6"
VOICE_CWD = Path.home() / ".cletus-voice"
CLAUDE_MD_PATH = Path.home() / "CLAUDE.md"

SESSION_FILE = VOICE_CWD / "session.json"
SESSION_MAX_IDLE_S = 60 * 60  # start a fresh conversation after an hour of quiet

# Short utterances that clear the conversation thread by voice.
RESET_PHRASES = ("fresh start", "new conversation", "clean slate", "start over")
RESET_MAX_LEN = 40  # only treat as a reset command when the utterance is this short


class RateLimited(Exception):
    """The upstream API returned a rate limit or a rate-limit-shaped error."""


def _looks_like_rate_limit(err: BaseException) -> bool:
    msg = str(err).lower()
    return (
        "rate limit" in msg
        or "429" in msg
        or "rate_limit" in msg
        or "too many requests" in msg
    )


def _subprocess_env(*, use_api_key: bool) -> dict[str, str]:
    """Build env for the spawned `claude` CLI.

    - use_api_key=True: keep ANTHROPIC_API_KEY, forces API-key path
    - use_api_key=False: strip API-key vars so CLI falls back to stored
      OAuth (Max plan)
    """
    env = dict(os.environ)
    if not use_api_key:
        env["ANTHROPIC_API_KEY"] = ""
        env["ANTHROPIC_AUTH_TOKEN"] = ""
    return env


class Brain:
    def __init__(self):
        VOICE_CWD.mkdir(parents=True, exist_ok=True)
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.has_max_plan_token = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        self.session_id: str | None = None
        self.last_exchange_at: float = 0.0
        self._load_session()

        pieces = []
        pieces.append("Max plan (stored OAuth)")
        if self.api_key:
            pieces.append("API key fallback")

        log.info(
            "brain initialized  sdk=claude-agent-sdk  model=%s  cwd=%s  auth=%s",
            MODEL,
            VOICE_CWD,
            " + ".join(pieces),
        )

        if CLAUDE_MD_PATH.exists():
            log.info("identity found at %s (auto-discovered by CLI)", CLAUDE_MD_PATH)
        else:
            log.warning("CLAUDE.md not found at %s; Cletus identity may be blank", CLAUDE_MD_PATH)

    # ------------------------------------------------------------------
    # Conversation session persistence
    # ------------------------------------------------------------------

    def _load_session(self) -> None:
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("could not read %s: %s", SESSION_FILE.name, e)
            return

        sid = data.get("session_id")
        ts = float(data.get("last_exchange_at", 0.0) or 0.0)
        if not sid:
            return
        idle = time.time() - ts
        if idle < SESSION_MAX_IDLE_S:
            self.session_id = sid
            self.last_exchange_at = ts
            log.info("resuming voice session %s  (idle %.0fs)", sid, idle)
        else:
            log.info("stored voice session expired (idle %.0fs), starting fresh", idle)

    def _save_session(self) -> None:
        try:
            SESSION_FILE.write_text(
                json.dumps(
                    {
                        "session_id": self.session_id,
                        "last_exchange_at": self.last_exchange_at,
                    }
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("could not write %s: %s", SESSION_FILE.name, e)

    def reset_session(self) -> None:
        self.session_id = None
        self.last_exchange_at = 0.0
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("voice session cleared")

    def _is_reset_command(self, user_text: str) -> bool:
        stripped = user_text.strip().lower().rstrip(".!?, ")
        if len(stripped) > RESET_MAX_LEN:
            return False
        return any(phrase in stripped for phrase in RESET_PHRASES)

    # ------------------------------------------------------------------
    # Thinking
    # ------------------------------------------------------------------

    async def think(self, user_text: str) -> str:
        if self._is_reset_command(user_text):
            self.reset_session()
            return "Fresh slate, boss. What's next?"

        if self.session_id and (time.time() - self.last_exchange_at) > SESSION_MAX_IDLE_S:
            log.info("voice session idle past limit, starting fresh")
            self.reset_session()

        # Path 1: Max plan (OAuth)
        try:
            return await self._call_with_session_retry(user_text, use_api_key=False)
        except RateLimited:
            log.warning("Max plan rate-limited, falling back to API key")
        except Exception as e:
            if _looks_like_rate_limit(e):
                log.warning("Max plan rate-limited (%s), falling back to API key", e)
            else:
                log.warning("Max plan call failed (%s), falling back to API key", e)

        if not self.api_key:
            return "My Max plan is capped and no API key is set for fallback. Set ANTHROPIC_API_KEY to keep me talking through rate limits."

        # Path 2: API key
        try:
            return await self._call_with_session_retry(user_text, use_api_key=True)
        except Exception as e:
            log.error("API key call also failed: %s", e)
            return f"My brain froze up: {e}"

    async def _call_with_session_retry(self, user_text: str, use_api_key: bool) -> str:
        """Call once; if a resume attempt fails for a non-rate-limit reason
        (stale or corrupt session file), clear the session and retry fresh."""
        had_session = self.session_id is not None
        try:
            return await self._call(user_text, use_api_key=use_api_key)
        except RateLimited:
            raise
        except Exception as e:
            if not had_session:
                raise
            log.warning("call with resume failed (%s); clearing session and retrying fresh", e)
            self.reset_session()
            return await self._call(user_text, use_api_key=use_api_key)

    async def _call(self, user_text: str, use_api_key: bool) -> str:
        auth_label = "api-key" if use_api_key else "max-plan"
        resume_id = self.session_id
        log.info(
            "brain -> %s%s: %r",
            auth_label,
            f"  (resume {resume_id})" if resume_id else "",
            user_text,
        )

        options = ClaudeAgentOptions(
            model=MODEL,
            cwd=VOICE_CWD,
            permission_mode="bypassPermissions",
            env=_subprocess_env(use_api_key=use_api_key),
            setting_sources=["user", "project", "local"],
            add_dirs=[str(Path.home()), str(Path.home() / "Documents" / "Brain")],
            resume=resume_id,
        )

        result_parts: list[str] = []
        rate_limited = False
        tool_calls = 0
        num_turns = 0
        total_cost = 0.0
        new_session_id: str | None = None

        try:
            async for message in query(prompt=user_text, options=options):
                if isinstance(message, RateLimitEvent):
                    status = message.rate_limit_info.status
                    if status == "rejected":
                        rate_limited = True
                        log.warning("RateLimitEvent (%s) rejected -> fallback", auth_label)
                        break
                    util = message.rate_limit_info.utilization
                    log.info(
                        "RateLimitEvent (%s) %s (util=%s), continuing on max plan",
                        auth_label, status, util,
                    )
                    continue
                if isinstance(message, AssistantMessage):
                    num_turns += 1
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_calls += 1
                            log.info("tool  %s", getattr(block, "name", "?"))
                elif isinstance(message, ResultMessage):
                    total_cost = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                    num_turns = int(getattr(message, "num_turns", num_turns) or num_turns)
                    new_session_id = getattr(message, "session_id", None) or new_session_id
                elif isinstance(message, SystemMessage):
                    pass
        except BaseException as e:
            if _looks_like_rate_limit(e):
                raise RateLimited() from e
            raise

        if rate_limited:
            raise RateLimited()

        if new_session_id:
            self.session_id = new_session_id
            self.last_exchange_at = time.time()
            self._save_session()

        log.info(
            "brain reply  auth=%s  session=%s  turns=%d  tools=%d  cost=$%.4f",
            auth_label, self.session_id, num_turns, tool_calls, total_cost,
        )

        text = "\n".join(p.strip() for p in result_parts if p and p.strip()).strip()
        return text or "(no reply text)"
