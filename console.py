"""Pure console-output parsing, redaction, and display formatting."""

import math
import re

# Anchored to "]: " so that player chat ("]: <Name> There are ...") cannot spoof results.
DEFAULT_LIST_PATTERN = r"\]: There are (\d+) of a max of (\d+) players online:\s*(.*)$"
DEFAULT_TPS_PATTERN = r"\]: Overall:\s*([\d.]+)\s*TPS\s*\(([\d.]+)\s*ms/tick\)"

_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COLOR_PATTERN = re.compile(r"§.")
# Lookarounds instead of \b: CJK characters count as word characters.
_IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# Require "::" or a hex letter so log timestamps like 12:34:56 are not matched.
_IPV6_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f:])(?=[0-9A-Fa-f:]*(?:::|[A-Fa-f]))"
    r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
)


def clean_log(text: str) -> list[str]:
    """Remove console styling and blank lines.

    Args:
        text: Raw console log text.

    Returns:
        Nonblank lines without styling or trailing whitespace.
    """
    text = _ANSI_PATTERN.sub("", text)
    text = _COLOR_PATTERN.sub("", text.replace("\r", ""))
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def diff_output(before: str, after: str) -> tuple[list[str], bool]:
    """Find new lines using the latest continuous tail anchor.

    Args:
        before: Log captured before sending the command.
        after: Log captured after waiting for command output.

    Returns:
        New lines and whether the previous output could not be anchored.
    """
    old_lines = clean_log(before)
    new_lines = clean_log(after)
    if not old_lines:
        return new_lines, False
    anchor = old_lines[-3:]
    width = len(anchor)
    for start in range(len(new_lines) - width, -1, -1):
        if new_lines[start : start + width] == anchor:
            return new_lines[start + width :], False
    return new_lines[-50:], True


def parse_list(lines: list[str], pattern: str) -> tuple[int, int, list[str]] | None:
    """Extract the latest valid online-player result.

    Args:
        lines: Cleaned console lines in chronological order.
        pattern: Expression capturing count, capacity, and comma-separated names.

    Returns:
        Online count, capacity, and names, or None when no result matches.
    """
    try:
        expression = re.compile(pattern)
    except re.error:
        return None
    for line in reversed(lines):
        match = expression.search(line)
        if match is None:
            continue
        try:
            count = int(match.group(1))
            maximum = int(match.group(2))
            names = match.group(3).split(",")
        except (IndexError, TypeError, ValueError, AttributeError):
            continue
        return count, maximum, [name.strip() for name in names if name.strip()]
    return None


def parse_tps(lines: list[str], pattern: str) -> tuple[float, float] | None:
    """Extract the latest finite TPS and tick-duration result.

    Args:
        lines: Cleaned console lines in chronological order.
        pattern: Expression capturing TPS and milliseconds per tick.

    Returns:
        TPS and milliseconds per tick, or None when no valid result matches.
    """
    try:
        expression = re.compile(pattern)
    except re.error:
        return None
    for line in reversed(lines):
        match = expression.search(line)
        if match is None:
            continue
        try:
            tps = float(match.group(1))
            milliseconds = float(match.group(2))
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(tps) and math.isfinite(milliseconds):
            return tps, milliseconds
    return None


def mask_ip(text: str) -> str:
    """Replace IPv4- and IPv6-shaped addresses with masked placeholders.

    Args:
        text: Text to redact.

    Returns:
        Text with IPv4 matches replaced by *.*.*.* and IPv6 matches by *:*.
    """
    return _IPV6_PATTERN.sub("*:*", _IPV4_PATTERN.sub("*.*.*.*", text))


def _nonnegative_number(value: object) -> float | None:
    """Convert a finite, nonnegative numeric field for display.

    Args:
        value: Optional, potentially malformed API field.

    Returns:
        A finite nonnegative number, or None for invalid fields.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def format_duration(milliseconds: object) -> str:
    """Format elapsed milliseconds as days, hours, and whole minutes.

    Args:
        milliseconds: Optional instance elapsed time in milliseconds.

    Returns:
        Human-readable duration, or 未知 for invalid fields.
    """
    number = _nonnegative_number(milliseconds)
    if number is None:
        return "未知"
    minutes = int(number // 60_000)
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days}天{hours}小时{minutes}分"


def format_memory(byte_count: object) -> str:
    """Format a byte count in MiB or GiB with one decimal place.

    Args:
        byte_count: Optional process memory usage in bytes.

    Returns:
        Human-readable memory usage, or 未知 for invalid fields.
    """
    number = _nonnegative_number(byte_count)
    if number is None:
        return "未知"
    if number >= 1024**3:
        return f"{number / 1024**3:.1f} GiB"
    return f"{number / 1024**2:.1f} MiB"
