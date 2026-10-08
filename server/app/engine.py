"""One merge, start to finish: read the source, plan where everything goes, write the output."""

from __future__ import annotations

from dataclasses import dataclass

from .ai import AIClient, AIError
from .matcher import build_plan
from .model import Document
from .readers.confluence import read_confluence
from .readers.docx_reader import read_docx
from .readers.pdf_reader import read_pdf
from .template import TemplateInfo, analyse_template
from .writer import build_preview, build_report, write_output


class SourceError(Exception):
    pass


@dataclass
class MergeResult:
    output: bytes
    report: dict
    preview: dict
    source_title: str


def read_source(kind: str, name: str, data: bytes | None, ref: str | None) -> Document:
    try:
        if kind == "confluence":
            return read_confluence(ref or "")
        lower = name.lower()
        if lower.endswith(".docx"):
            return read_docx(data or b"", name)
        if lower.endswith(".pdf"):
            return read_pdf(data or b"", name)
    except SourceError:
        raise
    except Exception as e:
        raise SourceError(f"Could not read {name}: {e}") from e
    raise SourceError(f"{name} is not a Word (.docx) or PDF file. Older .doc files need to be saved as .docx first.")


def merge(template_bytes: bytes, info: TemplateInfo | None, source: Document, source_name: str, provider: str | None) -> MergeResult:
    info = info or analyse_template(template_bytes)
    if not info.sections:
        raise SourceError("The template has no headings to merge into. Put section titles in Heading styles and upload it again.")
    if not source.blocks:
        raise SourceError(" ".join(source.notes) or f"No content could be read from {source_name}.")
    ai = None
    ai_note = ""
    if provider:
        try:
            ai = AIClient(provider)
        except AIError as e:
            ai_note = f"AI matching was skipped: {e}"
    plan = build_plan(info, source, ai)
    if ai_note:
        plan.notes.append(ai_note)
    output = write_output(template_bytes, info, plan, source_name)
    return MergeResult(output, build_report(info, plan), build_preview(info, plan, source.title), source.title)
