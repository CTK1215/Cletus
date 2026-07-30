"""Text cleanup helpers.

Chief job: strip Markdown formatting from Claude's replies before we hand
them to TTS, so the voice doesn't read literal backticks and asterisks.
"""

import re

_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^\*\n]+)\*(?!\*)")
_BOLD_UNDER = re.compile(r"__([^_]+)__")
_ITALIC_UNDER = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_UL_MARKER = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_OL_MARKER = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def strip_markdown(text: str) -> str:
    """Return a plain-text version of text suitable for speaking."""
    if not text:
        return ""
    t = text
    t = _CODE_BLOCK.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _BOLD.sub(r"\1", t)
    t = _ITALIC_STAR.sub(r"\1", t)
    t = _BOLD_UNDER.sub(r"\1", t)
    t = _ITALIC_UNDER.sub(r"\1", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _HEADER.sub("", t)
    t = _UL_MARKER.sub("", t)
    t = _OL_MARKER.sub("", t)
    t = _BLOCKQUOTE.sub("", t)
    t = _MULTI_SPACE.sub(" ", t)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def clamp_for_speech(text: str, max_chars: int = 1500) -> str:
    """Cap TTS input so a runaway reply doesn't lock up playback."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Try to end on a sentence boundary.
    for punct in [". ", "! ", "? ", "\n"]:
        idx = cut.rfind(punct)
        if idx > max_chars * 0.6:
            return cut[: idx + 1].strip()
    return cut.strip()
