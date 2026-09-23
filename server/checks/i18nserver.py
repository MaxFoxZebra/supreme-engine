"""Every message the server can put in front of someone has a translation.

The page shows the server's errors as it gets them, and translates them the
way it translates everything else: by looking the whole text up in the
catalogue, or matching one of its patterns. So a new `raise ValueError("…")`
or `{"error": "…"}` with no entry stays English in every language, and no
screen tour would find it, because it only shows when something goes wrong.

This reads the server's source instead: each raised message, each "error" and
"hint" in a response, each render hint, with the parts filled in at run time
standing in as a name. Those meant for a programmer rather than a person are
listed in INTERNAL.

    python checks/i18nserver.py
"""

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "i18n"))
from catalogue import C, RX  # noqa: E402

FILES = ["studio.py", "jobs.py", "importer.py", "letters.py", "languages.py", "backups.py"]
FILL = "Engineering"   # what a run-time part looks like, for matching a pattern

# For a programmer, never shown in the interface.
INTERNAL = {
    "the config file is not a JSON object", "the config file is not a YAML mapping",
    "unknown client: Engineering", "path outside the workspace", "not http", "too large",
    "unauthorised: supply X-API-Key", "bad request", "Unknown fix: Engineering",
    "Could not record the base CV in Engineering.", "Engineering: Engineering",
    "Typst mapping is unavailable in this build",
    # The reason in brackets after "…config file cannot be read": the file's own fault.
    '"mcpServers" is not a JSON object', '"mcp_servers" is not a YAML mapping',
}


def text(node) -> str | None:
    """The message a node builds, run-time parts filled in; None when it is
    not built from literal text at all."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else FILL for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = text(node.left), text(node.right)
        if a is None and b is None:
            return None
        return (a if a is not None else FILL) + (b if b is not None else FILL)
    if isinstance(node, ast.BoolOp):          # x or "fallback"
        for v in node.values:
            t = text(v)
            if t is not None:
                return t
    return None


def messages(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            yield node.lineno, text(node.exc.args[0])
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("error", "hint"):
                    yield node.lineno, text(v)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HINTS" for t in node.targets):
            for pair in node.value.elts:
                yield pair.lineno, text(pair.elts[1])


def known(s: str) -> bool:
    s = re.sub(r"\s+", " ", s).strip()
    return s in C or any(re.search(r[0], s) for r in RX)


missing = []
for name in FILES:
    for line, s in messages(HERE / name):
        if not s or s in INTERNAL or not re.search(r"[A-Za-z]{3}", s):
            continue
        if not known(s):
            missing.append(f"  MISS  {name}:{line}  {s!r}")
print("\n".join(missing) if missing else "every message the server sends is translated")
sys.exit(1 if missing else 0)
