"""Reads a text PDF into blocks. Headings are inferred from font size and weight, since PDFs carry no structure."""

from __future__ import annotations

import io
import re
from collections import Counter

import pdfplumber

from ..model import Block, Document, Run

BULLET = re.compile(r"^\s*(?:[•●▪◦‣\-–*]|\(?[0-9]{1,2}[.)]|\(?[a-z][.)])\s+")
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+[A-Z]")
LABEL_START = re.compile(r"^[A-Z][\w /&()-]{1,30}:\s+\S")
PAGE_NUMBER = re.compile(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", re.I)


def _lines(page) -> list[dict]:
    words = page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False, use_text_flow=True)
    tables = []
    try:
        tables = page.find_tables()
    except Exception:
        pass
    boxes = [t.bbox for t in tables]

    def in_table(w):
        return any(b[0] - 1 <= w["x0"] and w["x1"] <= b[2] + 1 and b[1] - 1 <= w["top"] and w["bottom"] <= b[3] + 1 for b in boxes)

    lines: list[dict] = []
    for w in words:
        if in_table(w):
            continue
        if lines and abs(lines[-1]["top"] - w["top"]) < 2.5:
            ln = lines[-1]
            ln["words"].append(w)
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "words": [w]})
    out = []
    for ln in lines:
        ws = ln["words"]
        text = " ".join(w["text"] for w in ws).strip()
        chars = sum(len(w["text"]) for w in ws)
        bold_chars = sum(len(w["text"]) for w in ws if re.search(r"bold|black|heavy|semibold", w["fontname"], re.I))
        size = round(max(w["size"] for w in ws), 1)
        out.append({"text": text, "top": ln["top"], "bottom": max(w["bottom"] for w in ws), "size": size,
                    "bold": chars > 0 and bold_chars / chars > 0.6, "x0": min(w["x0"] for w in ws), "chars": chars})
    table_blocks = []
    for t in tables:
        try:
            data = t.extract()
        except Exception:
            continue
        rows = [[[Run((c or "").strip())] if (c or "").strip() else [] for c in row] for row in data if row and any((c or "").strip() for c in row)]
        if rows:
            table_blocks.append({"top": t.bbox[1], "table": rows})
    return out, table_blocks


def read_pdf(data: bytes, filename: str) -> Document:
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        meta_title = (pdf.metadata or {}).get("Title") or ""
        for page in pdf.pages:
            lines, tables = _lines(page)
            pages.append((lines, tables, page.height))
    if not any(lines for lines, _, _ in pages):
        return Document(title=filename, blocks=[], kind="pdf", notes=["No text could be read from this PDF. It may be a scanned image; it needs OCR before it can be merged."])

    # Repeated headers/footers: the same text (digits ignored) in the top/bottom margin of most pages.
    edge = Counter()
    for lines, _, h in pages:
        seen = set()
        for ln in lines:
            if ln["top"] < h * 0.08 or ln["bottom"] > h * 0.92:
                key = re.sub(r"\d+", "#", ln["text"].lower())
                if key not in seen:
                    edge[key] += 1
                    seen.add(key)
    repeated = {k for k, n in edge.items() if len(pages) > 1 and n >= max(2, len(pages) * 0.5)}

    sizes = Counter()
    for lines, _, _ in pages:
        for ln in lines:
            sizes[ln["size"]] += ln["chars"]
    body = sizes.most_common(1)[0][0]
    heading_sizes = sorted({ln["size"] for lines, _, _ in pages for ln in lines if ln["size"] >= body + 1.5}, reverse=True)

    def level_for(ln) -> int | None:
        text = ln["text"]
        if len(text) > 120 or text.endswith((".", ",", ";")) and not NUMBERED_HEADING.match(text):
            return None
        if ln["size"] in heading_sizes:
            return min(heading_sizes.index(ln["size"]) + 1, 6)
        m = NUMBERED_HEADING.match(text)
        if ln["bold"] and len(text) < 90:
            if m:
                return min(m.group(1).count(".") + 1, 6)
            return min(len(heading_sizes) + 1, 6)
        return None

    blocks: list[Block] = []
    title = ""
    para: list[str] = []
    para_bold = False
    last_bottom = None
    line_gap = body * 1.6

    def flush():
        nonlocal para, para_bold
        if para:
            text = re.sub(r"\s+", " ", " ".join(para)).strip()
            text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # rejoin hyphenated line breaks
            m = BULLET.match(text)
            if m:
                ordered = bool(re.match(r"^\s*\(?[0-9a-z]{1,2}[.)]", m.group(0)))
                blocks.append(Block("list", runs=[Run(text[m.end():], bold=para_bold)], ordered=ordered))
            else:
                blocks.append(Block("para", runs=[Run(text, bold=para_bold)]))
        para, para_bold = [], False

    for pi, (lines, tables, h) in enumerate(pages):
        pending_tables = sorted(tables, key=lambda t: t["top"])
        last_bottom = None
        for ln in lines:
            text = ln["text"]
            while pending_tables and pending_tables[0]["top"] < ln["top"]:
                flush()
                blocks.append(Block("table", rows=pending_tables.pop(0)["table"]))
            if re.sub(r"\d+", "#", text.lower()) in repeated or PAGE_NUMBER.match(text):
                continue
            lvl = level_for(ln)
            if lvl:
                flush()
                if not title and pi == 0 and lvl == 1 and not blocks:
                    title = text
                else:
                    blocks.append(Block("heading", runs=[Run(text)], level=lvl))
                last_bottom = ln["bottom"]
                continue
            gap = (ln["top"] - last_bottom) if last_bottom is not None else 0
            if para and (gap > line_gap or BULLET.match(text) or LABEL_START.match(text)):
                flush()
            if not para:
                para_bold = ln["bold"]
            para.append(text)
            last_bottom = ln["bottom"]
        flush()
        for t in pending_tables:
            blocks.append(Block("table", rows=t["table"]))

    # Normalise levels so the shallowest heading is level 1.
    levels = sorted({b.level for b in blocks if b.kind == "heading"})
    remap = {lv: i + 1 for i, lv in enumerate(levels)}
    for b in blocks:
        if b.kind == "heading":
            b.level = remap[b.level]
    notes = ["PDF structure is inferred from font sizes, so check that headings were recognised correctly."]
    return Document(title=title or meta_title or re.sub(r"\.[^.]+$", "", filename), blocks=blocks, kind="pdf", notes=notes)
