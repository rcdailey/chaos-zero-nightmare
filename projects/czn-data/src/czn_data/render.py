"""Plain-text rendering of MCP tool results.

Only Claude reads MCP results (programs use the REST routes), and JSON's braces, quotes, escapes
and repeated keys cost tokens without helping it. Rules, applied recursively:

- A mapping is one ``key: value`` line per field. A nested mapping or list of mappings goes
  under ``key:``, indented.
- A list of mappings is one line per item, starting ``- ``: the first field's value unlabeled
  when it is text (the item's name), then ``key: value`` fields joined by ``; ``. Long or
  multi-line text and nested mappings and lists of mappings go on indented lines below it.
- Fields with the same value in every item of a list (source URL, fetch time) are stated once
  above the items, on an ``every item:`` line.
- A list of scalars joins with ``, `` (a short list) or is one ``- `` line per value.
- Datetimes are UTC to the minute; empty values are left out by the tools, so absent means
  unknown or none.
"""

from datetime import UTC, date, datetime

# Text longer than this, or with line breaks beyond SHORT_LINES, goes on its own lines.
INLINE_CHARS = 160
SHORT_LINES = 3
INDENT = "  "


def render(value: object) -> str:
    if value is None:
        return "No match."
    if isinstance(value, list):
        return "\n".join(_list(value, "")) if value else "No results."
    if isinstance(value, dict):
        return "\n".join(_mapping(value, ""))
    return _scalar(value)


def _scalar(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _is_record(value: object) -> bool:
    return isinstance(value, dict) or (
        isinstance(value, list) and any(isinstance(item, dict) for item in value)
    )


def _short_text(value: object) -> str | None:
    """value on one line, or None when it is too long for that."""
    if value == []:
        return "none"
    if isinstance(value, list):
        parts = [_scalar(item) for item in value]
        if any(", " in part for part in parts):
            return None
        text = ", ".join(parts)
    else:
        text = _scalar(value)
        lines = text.splitlines()
        if len(lines) > SHORT_LINES:
            return None
        text = " / ".join(line.strip() for line in lines if line.strip())
    return text if len(text) <= INLINE_CHARS else None


def _field(key: str, value: object, indent: str) -> list[str]:
    """One field of a mapping as lines at indent."""
    if _is_record(value):
        body = _list(value, indent + INDENT) if isinstance(value, list) else None
        return [f"{indent}{key}:", *(body or _mapping(value, indent + INDENT))]
    short = _short_text(value)
    if short is not None:
        return [f"{indent}{key}: {short}"]
    if isinstance(value, list):
        return [f"{indent}{key}:", *(f"{indent}{INDENT}- {_scalar(v)}" for v in value)]
    return [f"{indent}{key}:", *_block(_scalar(value), indent + INDENT)]


def _block(text: str, indent: str) -> list[str]:
    return [f"{indent}{line.strip()}" for line in text.splitlines() if line.strip()]


def _mapping(mapping: dict, indent: str) -> list[str]:
    return [line for key, value in mapping.items() for line in _field(key, value, indent)]


def _list(items: list, indent: str) -> list[str]:
    if not all(isinstance(item, dict) for item in items):
        return [f"{indent}- {_scalar(item)}" for item in items]
    shared = _shared_fields(items)
    lines = []
    if shared:
        lines.append(
            f"{indent}every item: " + "; ".join(f"{k}: {_short_text(v)}" for k, v in shared.items())
        )
    for item in items:
        lines += _item({k: v for k, v in item.items() if k not in shared}, indent)
    return lines


def _name_key(item: dict) -> str | None:
    """The field that names item, written first and unlabeled: name or title, or else a first
    field holding text."""
    for key in ("name", "title"):
        if key in item:
            return key
    first = next(iter(item), None)
    return first if isinstance(item.get(first), str) else None


def _shared_fields(items: list[dict]) -> dict:
    """Short scalar fields equal in every item, including a name they all share."""
    if len(items) < 2:
        return {}
    first = items[0]
    return {
        key: value
        for key, value in first.items()
        if not _is_record(value)
        and _short_text(value) is not None
        and all(key in item and item[key] == value for item in items[1:])
    }


def _item(item: dict, indent: str) -> list[str]:
    name = _name_key(item)
    inline, below = [], []
    if name is not None and (short := _short_text(item[name])) is not None:
        inline.append(short)
        item = {k: v for k, v in item.items() if k != name}
    for key, value in item.items():
        short = None if _is_record(value) else _short_text(value)
        if short is None:
            below += _field(key, value, indent + INDENT)
        else:
            inline.append(f"{key}: {short}")
    return [f"{indent}- {'; '.join(inline)}".rstrip(), *below]
