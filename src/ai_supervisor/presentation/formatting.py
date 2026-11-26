from __future__ import annotations

import html
import re
from collections.abc import Iterable

# Telegram supports a deliberately small HTML subset. All user/provider text is escaped first.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def heading(text: str) -> str:
    return f"<b>{html.escape(text, quote=True)}</b>"


def paragraph(text: str) -> str:
    return html.escape(text.strip(), quote=True)


def section(title: str, body: str) -> str:
    """One visual block: heading followed by a blank line, never glued to prior content."""
    clean = body.strip()
    return f"{heading(title)}\n\n{clean}" if clean else heading(title)


def metadata(items: Iterable[tuple[str, str]]) -> str:
    return "\n".join(
        f"{html.escape(label, quote=True)}: {html.escape(value, quote=True)}"
        for label, value in items
    )


def bullets(items: Iterable[str], *, long_threshold: int = 90) -> str:
    rendered: list[str] = []
    for item in items:
        line = f"• {html.escape(str(item), quote=True)}"
        if len(line) >= long_threshold and rendered:
            rendered.append("")
        rendered.append(line)
    return "\n".join(rendered)


def _protect_code(text: str) -> tuple[str, list[str]]:
    blocks: list[str] = []

    def replace(match: re.Match[str]) -> str:
        blocks.append(match.group(1))
        return f"\x00CODE{len(blocks) - 1}\x00"

    return re.sub(r"```[^\n]*\n?(.*?)```", replace, text, flags=re.DOTALL), blocks


def markdown_to_telegram_html(text: str) -> str:
    """Safe Markdown subset -> Telegram HTML; preserves blank lines and code blocks."""
    if not text:
        return ""
    protected, blocks = _protect_code(text)
    inline: list[str] = []

    def protect_inline(match: re.Match[str]) -> str:
        inline.append(match.group(1))
        return f"\x01INLINE{len(inline) - 1}\x01"

    protected = _INLINE_CODE.sub(protect_inline, protected)
    escaped = html.escape(protected, quote=True)
    escaped = _HEADING.sub(lambda m: f"<b>{m.group(1).strip()}</b>", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC.sub(r"<i>\1</i>", escaped)
    escaped = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)
    escaped = re.sub(r"^[ \t]*[-*][ \t]+", "• ", escaped, flags=re.MULTILINE)
    for index, value in enumerate(inline):
        escaped = escaped.replace(
            f"\x01INLINE{index}\x01", f"<code>{html.escape(value, quote=True)}</code>"
        )
    for index, value in enumerate(blocks):
        escaped = escaped.replace(
            f"\x00CODE{index}\x00",
            f"<pre><code>{html.escape(value, quote=True)}</code></pre>",
        )
    escaped = re.sub(r"\n{3,}", "\n\n", escaped)
    return escaped.strip()


def normalize_card(text: str) -> str:
    """Keep intentional blank lines, remove accidental triple spacing."""
    return re.sub(r"\n{3,}", "\n\n", text.strip())
