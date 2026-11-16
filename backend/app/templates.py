"""Sandbox templates. Each maps to an image built from images/<name>/Dockerfile."""

from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    preview_port: int
    check_hint: str = ""  # a typical checks command, suggested (never set) in the UI

    @property
    def image(self) -> str:
        return f"agent-hub/{self.name}:{config.settings.image_version}"


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in (
        Template("python", "Python 3.13, pip, venv, git, build tools", 8000, "python -m pytest -q"),
        Template("node", "Node.js 24 LTS, npm, git", 5173, "npm test"),
        Template("static", "Lightweight Debian with git and a static file server", 3000),
    )
}
