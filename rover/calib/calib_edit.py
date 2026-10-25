"""Edit calib.yaml in place, keeping its comments.

Shared by `calibrate_camera.py` and `calibrate_motion.py`. Deliberately textual rather than
a PyYAML round trip: calib.yaml is more than half comments, and the comments are the part
that says which numbers are measurements and which are placeholders. A round trip would
silently delete every one of them, which is worse than the drift it would prevent.

Every function takes the file's text and returns new text; nothing here touches the disk, so
it is all testable without a Pi.
"""

from __future__ import annotations

import re

__all__ = ["CalibEditError", "replace_scalar", "replace_table", "set_calibrated_at", "fmt_num"]


class CalibEditError(ValueError):
    """The key is not in the file, so writing would silently lose a measurement."""


def fmt_num(value: float) -> str:
    """Short, round-trippable, and never scientific notation (which YAML reads as a string)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def replace_scalar(text: str, key: str, value: float, comment: str = "") -> str:
    """`  key: &lt;old&gt;   # old comment` -> `  key: &lt;value&gt;   # comment`, same indent."""
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*){re.escape(key)}:[ \t]*[^\r\n#]*(?P<rest>#[^\r\n]*)?$", re.MULTILINE
    )
    match = pattern.search(text)
    if match is None:
        raise CalibEditError(f"{key} is not in calib.yaml")
    tail = (
        f"   # {comment}"
        if comment
        else (f"   {match.group('rest')}" if match.group("rest") else "")
    )
    return (
        text[: match.start()]
        + f"{match.group('indent')}{key}: {fmt_num(value)}{tail}"
        + text[match.end() :]
    )


def replace_table(text: str, key: str, table: dict[float, float], comment: str = "") -> str:
    """Replace a nested `key:` mapping of number -> number, e.g. `m_per_s_at`.

    Matches the key line plus every indented `&lt;number&gt;: &lt;number&gt;` row under it (and any
    comment rows mixed in), so the replacement cannot leave half of the old table behind --
    which would be a table that parses fine and is wrong.
    """
    if not table:
        raise CalibEditError(f"refusing to write an empty {key}")
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*){re.escape(key)}:[^\r\n]*"
        r"(?:\r?\n(?:[ \t]+[0-9][0-9.eE+-]*:[^\r\n]*|[ \t]+#[^\r\n]*))+",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match is None:
        raise CalibEditError(f"{key} is not in calib.yaml, or is not a nested table")
    indent = match.group("indent")
    head = f"{indent}{key}:" + (f"   # {comment}" if comment else "")
    rows = "\n".join(f"{indent}  {fmt_num(k)}: {fmt_num(v)}" for k, v in sorted(table.items()))
    return text[: match.start()] + head + "\n" + rows + text[match.end() :]


def set_calibrated_at(text: str, when: str, what: str = "") -> str:
    """Stamp `calibrated_at`. This is the flag the runtime reads to decide what it may trust."""
    pattern = re.compile(r"^calibrated_at:[^\r\n]*", re.MULTILINE)
    if pattern.search(text) is None:
        raise CalibEditError("calibrated_at is not in calib.yaml")
    note = f"   # set by {what}" if what else ""
    return pattern.sub(f'calibrated_at: "{when}"{note}', text, count=1)
