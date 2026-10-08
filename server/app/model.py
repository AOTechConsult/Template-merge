"""The shared shape every reader produces: a flat list of blocks with light formatting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Literal


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class Block:
    kind: Literal["heading", "para", "list", "table"]
    runs: list[Run] = field(default_factory=list)
    level: int = 0  # heading level (1-6) or list depth (0-based)
    ordered: bool = False  # numbered list item
    rows: list[list[list[Run]]] = field(default_factory=list)  # table: rows -> cells -> runs

    @property
    def text(self) -> str:
        if self.kind == "table":
            return "\n".join(" | ".join(cell_text(c) for c in row) for row in self.rows)
        return "".join(r.text for r in self.runs)

    def to_json(self) -> dict:
        return asdict(self)


def cell_text(runs: list[Run]) -> str:
    return "".join(r.text for r in runs)


@dataclass
class Document:
    title: str
    blocks: list[Block]
    kind: str  # docx | pdf | confluence
    notes: list[str] = field(default_factory=list)


@dataclass
class Section:
    """A heading and everything under it until the next heading (of any level)."""

    id: str
    title: str
    level: int
    body: list[Block]
    parent: str | None = None  # id of the nearest higher-level section
    index: int = 0

    @property
    def path(self) -> list[str]:
        return []

    @property
    def body_text(self) -> str:
        return "\n".join(b.text for b in self.body)


def split_sections(doc: Document) -> list[Section]:
    """Splits a document at every heading. Content before the first heading becomes a preamble."""
    sections: list[Section] = []
    stack: list[Section] = []
    current = Section(id="src0", title="", level=0, body=[], index=0)
    for b in doc.blocks:
        if b.kind == "heading" and b.text.strip():
            if current.title or current.body:
                sections.append(current)
            while stack and stack[-1].level >= b.level:
                stack.pop()
            current = Section(
                id=f"src{len(sections) + 1}",
                title=b.text.strip(),
                level=b.level,
                body=[],
                parent=stack[-1].id if stack else None,
                index=len(sections) + 1,
            )
            stack.append(current)
        else:
            current.body.append(b)
    if current.title or current.body:
        sections.append(current)
    return sections


_NUMBERING = re.compile(r"^\s*(?:(?:section|article|part)\s+)?(?:[0-9]+(?:\.[0-9]+)*|[ivxlc]+|[a-z])[.)]?\s+", re.I)


def clean_heading(text: str) -> str:
    """'3.2 Roles & Responsibilities:' -> 'roles and responsibilities'"""
    t = _NUMBERING.sub("", text.strip())
    t = t.replace("&", " and ").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def plain(text: str) -> list[Run]:
    return [Run(text)] if text else []
