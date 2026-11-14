"""Canned data so DEMO_MODE can show the Files, Changes and Preview tabs without Docker."""

from __future__ import annotations

from typing import Any

from .db import Run, Sandbox
from .preview import preview_url
from .templates import TEMPLATES

README = "# Demo project\n\nA small example.\n\n## About this project\n\nIt greets people. `src/app.py` holds the logic.\n"
APP = 'def greet(name: str) -> str:\n    """Say hello."""\n    return f"Hello, {name}!"\n\n\nif __name__ == "__main__":\n    print(greet("world"))\n'
TREE: dict[str, list[dict[str, Any]]] = {
    "": [{"name": ".git", "type": "dir", "size": 4096}, {"name": "src", "type": "dir", "size": 4096},
         {"name": "tests", "type": "dir", "size": 4096}, {"name": "README.md", "type": "file", "size": len(README)},
         {"name": "pyproject.toml", "type": "file", "size": 212}],
    "src": [{"name": "app.py", "type": "file", "size": len(APP)}],
    "tests": [{"name": "test_app.py", "type": "file", "size": 96}],
}
CONTENT = {"README.md": README, "src/app.py": APP, "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
           "tests/test_app.py": "from src.app import greet\n\n\ndef test_greet():\n    assert greet('x') == 'Hello, x!'\n"}

PATCH = """diff --git a/README.md b/README.md
index 1a2b3c4..5d6e7f8 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,7 @@
 # Demo project
 
 A small example.
+
+## About this project
+
+It greets people. `src/app.py` holds the logic.
diff --git a/src/app.py b/src/app.py
index 2b3c4d5..6e7f8a9 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,5 +1,6 @@
 def greet(name: str) -> str:
-    return "Hello, " + name
+    \"\"\"Say hello.\"\"\"
+    return f"Hello, {name}!"
 
 
 if __name__ == "__main__":
"""


def browse(path: str) -> dict[str, Any]:
    path = path.strip("/")
    if path in TREE:
        return {"path": path, "kind": "dir", "size": 4096, "entries": TREE[path], "truncated": False}
    if path in CONTENT:
        return {"path": path, "kind": "file", "size": len(CONTENT[path]), "content": CONTENT[path]}
    from .files import PathError
    raise PathError("No such file or directory")


def diff(run: Run) -> dict[str, Any]:
    return {"run": run.id, "base": run.base_branch or "main", "branch": run.branch, "size": len(PATCH),
            "files": [{"path": "README.md", "added": 4, "removed": 0}, {"path": "src/app.py", "added": 2, "removed": 1}],
            "patch": PATCH}


def preview(sb: Sandbox) -> dict[str, Any]:
    return {"url": preview_url(sb) or "http://demo.example.ts.net:8200/", "port": TEMPLATES[sb.template].preview_port,
            "host_port": sb.preview_port or 8200, "enabled": True,
            "listening": "any" if sb.state == "running" else "stopped", "static_server": False,
            "has_index": False, "lists_directories": sb.template == "python"}
