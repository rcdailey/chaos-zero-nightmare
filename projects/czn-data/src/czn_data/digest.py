"""Fingerprints that tell a step whether an item's input or the step's own code changed since
the item was last processed, so a run reprocesses only what changed.

A step records each item's fingerprint in the ``processed`` table after processing it cleanly,
and skips the item while the fingerprint matches. The fingerprint combines the item's input with
``code()`` of the modules that turn input into output.

``code()`` hashes the source of the named modules and of every ``czn_data`` module they import,
directly or not, plus the installed version of each third-party package those modules import and
the Python version. It cannot see data files a module reads (pass their contents to ``item()``
instead) or behavior that comes from a package the closure does not import itself. A module that
imports a large part of the package (such as ``czn_data.game8``, which imports ``scrape``)
reprocesses its items after edits to any of those modules too; that costs time once, never
correctness.
"""

import ast
import hashlib
import importlib.util
import platform
import sys
from importlib.metadata import packages_distributions, version
from pathlib import Path

import psycopg

PACKAGE = __package__


def code(*modules: str) -> str:
    """Fingerprint of the code of ``modules`` (dotted names) and what they import."""
    sources: dict[str, bytes] = {}
    packages: set[str] = set()
    pending = list(modules)
    while pending:
        name = pending.pop()
        if name in sources:
            continue
        path = Path(importlib.util.find_spec(name).origin)
        sources[name] = path.read_bytes()
        for imported in _imports(name, path, sources[name]):
            top = imported.partition(".")[0]
            if top == PACKAGE:
                pending.append(imported)
            elif top not in sys.stdlib_module_names:
                packages.add(top)
    distributions = packages_distributions()
    versions = sorted(
        f"{dist}=={version(dist)}" for top in packages for dist in distributions.get(top, [])
    )
    return item(platform.python_version(), *versions, *(sources[n] for n in sorted(sources)))


def _imports(name: str, path: Path, source: bytes) -> set[str]:
    """Dotted names of the modules ``name`` imports; ``from . import x`` resolves to module
    ``x`` when it is one, else to the package."""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0]
                base = f"{parent}.{base}" if base else parent
            is_package = base.startswith(PACKAGE) and _is_package(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                is_module = is_package and importlib.util.find_spec(candidate) is not None
                found.add(candidate if is_module else base)
    return found


def _is_package(name: str) -> bool:
    return importlib.util.find_spec(name).submodule_search_locations is not None


def item(*parts: str | bytes | None) -> str:
    """Fingerprint of an item's input parts, in order."""
    hasher = hashlib.sha256()
    for part in parts:
        data = b"\x00" if part is None else part if isinstance(part, bytes) else part.encode()
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def recorded(conn: psycopg.Connection, step: str) -> dict[str, tuple[str, bytes | None]]:
    """Fingerprint and stored output of each item ``step`` last processed cleanly, by key."""
    return {
        key: (fingerprint, value)
        for key, fingerprint, value in conn.execute(
            "SELECT key, digest, value FROM processed WHERE step = %s", (step,)
        )
    }


def record(
    conn: psycopg.Connection, step: str, key: str, fingerprint: str, value: bytes | None = None
) -> None:
    """Remember that ``step`` processed item ``key`` cleanly from input ``fingerprint``, with the
    output ``value`` when the step reuses its output rather than rows it stored elsewhere."""
    conn.execute(
        "INSERT INTO processed (step, key, digest, value) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (step, key) DO UPDATE SET digest = excluded.digest, value = excluded.value",
        (step, key, fingerprint, value),
    )


def forget_others(conn: psycopg.Connection, step: str, keys: list[str]) -> None:
    """Drop what ``step`` remembers about items no longer among ``keys`` (removed sources)."""
    conn.execute("DELETE FROM processed WHERE step = %s AND NOT key = ANY (%s)", (step, keys))
