"""Writes the merged document: a copy of the template with each section filled from the source.

The template's styles, page setup, headers, footers and fixed wording are kept. Missing sections and
unfilled fields are flagged in bold red with a yellow highlight, and review comments explain each flag.
"""

from __future__ import annotations

import copy
import io
import re

import docx
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

from .matcher import MergePlan
from .model import Block, Run
from .readers.docx_reader import heading_level, iter_body
from .template import TemplateInfo, is_guidance, is_title_para

RED = RGBColor(0xC0, 0x00, 0x00)
AUTHOR = "Template Merge"


def _style(doc, name: str):
    try:
        return doc.styles[name]
    except KeyError:
        return None


def _new_paragraph(doc, anchor) -> Paragraph:
    p = OxmlElement("w:p")
    anchor.addnext(p)
    return Paragraph(p, doc._body)


def _add_runs(p: Paragraph, runs: list[Run]) -> None:
    for r in runs:
        parts = r.text.split("\n")
        for i, part in enumerate(parts):
            run = p.add_run(part)
            if r.bold:
                run.bold = True
            if r.italic:
                run.italic = True
            if r.underline:
                run.underline = True
            if i < len(parts) - 1:
                run.add_break()


def _flag(run) -> None:
    run.bold = True
    run.font.color.rgb = RED
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _comment(doc, runs, text: str) -> None:
    runs = [r for r in runs if r.text]
    if not runs:
        return
    try:
        doc.add_comment(runs, text=text, author=AUTHOR, initials="TM")
    except Exception:
        pass  # comments are a convenience; never fail the merge over one


def _borders(tbl) -> None:
    tblPr = tbl._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tblPr.append(borders)


class _Writer:
    def __init__(self, doc, body_style: str | None):
        self.doc = doc
        self.body_style = body_style
        self.list_counters: dict[int, int] = {}

    def heading(self, anchor, text: str, level: int):
        p = _new_paragraph(self.doc, anchor)
        st = _style(self.doc, f"Heading {min(max(level, 1), 9)}")
        if st is not None:
            p.style = st
            p.add_run(text)
        else:
            r = p.add_run(text)
            r.bold = True
            r.font.size = Pt(max(11, 16 - 2 * (level - 1)))
        return p._p, p

    def block(self, anchor, b: Block):
        """Inserts one block after anchor; returns the new last element and the first paragraph (for comments)."""
        if b.kind == "heading":
            self.list_counters.clear()
            return self.heading(anchor, b.text, b.level)
        if b.kind == "table":
            self.list_counters.clear()
            ncols = max((len(r) for r in b.rows), default=1)
            t = self.doc.add_table(rows=len(b.rows), cols=ncols)
            st = _style(self.doc, "Table Grid")
            if st is not None:
                t.style = st
            else:
                _borders(t)
            for ri, row in enumerate(b.rows):
                for ci in range(ncols):
                    cell = t.cell(ri, ci)
                    runs = row[ci] if ci < len(row) else []
                    _add_runs(cell.paragraphs[0], [Run(r.text, r.bold or ri == 0, r.italic, r.underline) for r in runs])
            anchor.addnext(t._tbl)
            return t._tbl, None
        p = _new_paragraph(self.doc, anchor)
        if b.kind == "list":
            depth = min(b.level, 2)
            name = ("List Number" if b.ordered else "List Bullet") + (f" {depth + 1}" if depth else "")
            st = _style(self.doc, name)
            if st is not None:
                p.style = st
                _add_runs(p, b.runs)
            else:
                if self.body_style and _style(self.doc, self.body_style) is not None:
                    p.style = self.body_style
                n = self.list_counters.get(depth, 0) + 1
                self.list_counters[depth] = n
                for d in list(self.list_counters):
                    if d > depth:
                        del self.list_counters[d]
                p.paragraph_format.left_indent = Pt(18 * (depth + 1))
                p.paragraph_format.first_line_indent = Pt(-12)
                p.add_run(f"{n}. " if b.ordered else "•  ")
                _add_runs(p, b.runs)
            return p._p, p
        self.list_counters.clear()
        if self.body_style and _style(self.doc, self.body_style) is not None:
            p.style = self.body_style
        _add_runs(p, b.runs)
        return p._p, p


def _replace_token(p: Paragraph, token: str, value: str | None, label: str) -> int:
    """Replaces token in a paragraph even when Word split it across runs. value None = flag as missing."""
    count = 0
    for _ in range(20):
        runs = p.runs
        texts = [r.text for r in runs]
        full = "".join(texts)
        pos = full.find(token)
        if pos < 0:
            break
        end = pos + len(token)
        acc, i, j, oi, oj = 0, None, None, 0, 0
        for k, t in enumerate(texts):
            if i is None and pos < acc + len(t):
                i, oi = k, pos - acc
            if j is None and end <= acc + len(t):
                j, oj = k, end - acc
                break
            acc += len(t)
        if i is None or j is None:
            break
        before, after = texts[i][:oi], texts[j][oj:]
        base = runs[i]._r
        runs[i].text = before
        for k in range(i + 1, j + 1):
            runs[k].text = ""
        new_r = copy.deepcopy(base)
        for t in new_r.findall(qn("w:t")):
            new_r.remove(t)
        base.addnext(new_r)
        from docx.text.run import Run as DocxRun

        nr = DocxRun(new_r, p)
        nr.text = value if value is not None else f"[MISSING: {label}]"
        if value is None:
            _flag(nr)
        if after:
            tail = copy.deepcopy(runs[j]._r)
            new_r.addnext(tail)
            DocxRun(tail, p).text = after
        count += 1
    return count


def _all_paragraphs(doc):
    def walk_tables(tables):
        for t in tables:
            for row in t.rows:
                for c in row.cells:
                    yield from c.paragraphs
                    yield from walk_tables(c.tables)

    yield from doc.paragraphs
    yield from walk_tables(doc.tables)
    for s in doc.sections:
        for part in (s.header, s.footer, s.first_page_header, s.first_page_footer, s.even_page_header, s.even_page_footer):
            try:
                if part.is_linked_to_previous and part is not s.header and part is not s.footer:
                    continue
                yield from part.paragraphs
                yield from walk_tables(part.tables)
            except Exception:
                continue


def write_output(template_bytes: bytes, info: TemplateInfo, plan: MergePlan, source_name: str) -> bytes:
    doc = docx.Document(io.BytesIO(template_bytes))
    items = list(iter_body(doc))
    elements = [it._p if isinstance(it, Paragraph) else it._tbl for it in items]
    heading_idx = [s.element_index for s in info.sections]

    # ---------- sections, last to first so earlier positions stay valid ----------
    for n in range(len(info.sections) - 1, -1, -1):
        sec = info.sections[n]
        start = sec.element_index
        stop = heading_idx[n + 1] if n + 1 < len(heading_idx) else len(items)
        heading_p = items[start]
        region = items[start + 1:stop]
        guidance = [it for it in region if isinstance(it, Paragraph) and is_guidance(it)]
        kept = [it for it in region if it not in guidance and not (isinstance(it, Paragraph) and not it.text.strip() and it is region[-1])]
        anchor = (kept[-1]._p if isinstance(kept[-1], Paragraph) else kept[-1]._tbl) if kept else heading_p._p

        body_style = None
        for it in region:
            if isinstance(it, Paragraph) and it.text.strip() and not heading_level(it):
                nm = it.style.name if it.style is not None else None
                if nm and not re.search(r"instruction|guidance|hint|placeholder|comment", nm, re.I) and not nm.lower().startswith("list"):
                    body_style = nm
                    break
        w = _Writer(doc, body_style or "Normal")
        status = plan.status.get(sec.key)
        placements = plan.placements.get(sec.key, [])

        if placements:
            for g in guidance:
                g._p.getparent().remove(g._p)
            for pl in placements:
                first_para = None
                for b in pl.blocks:
                    anchor, para = w.block(anchor, b)
                    if first_para is None and para is not None:
                        first_para = para
                if pl.method == "ai" and first_para is not None:
                    _comment(doc, first_para.runs, f"Placed here by AI from '{pl.source_title}'. {pl.note}. Please confirm.")
        elif status == "missing":
            for g in guidance:
                for r in g.runs:
                    _flag(r)
            p = _new_paragraph(doc, anchor)
            r1 = p.add_run("Missing from the source document. ")
            _flag(r1)
            r2 = p.add_run(f"No content for this section was found in {source_name}. Add it, or record that it does not apply.")
            r2.bold = True
            r2.font.color.rgb = RED
            _comment(doc, heading_p.runs, f"Missing: '{sec.title}' is in the template but not in {source_name}.")
        elif status == "template_text":
            _comment(doc, heading_p.runs, f"Not in {source_name}; the template's standard wording was kept. Confirm it applies.")
        elif status == "covered":
            for g in guidance:
                g._p.getparent().remove(g._p)

    # ---------- content that matched nothing ----------
    if plan.extra:
        body = doc.element.body
        last = [c for c in body.iterchildren() if c.tag in (qn("w:p"), qn("w:tbl"))][-1]
        w = _Writer(doc, "Normal")
        last, hp = w.heading(last, "Content not in the template", 1)
        _comment(doc, hp.runs, f"These parts of {source_name} did not match any template section. Move them into the right section or delete them.")
        note = _new_paragraph(doc, last)
        nr = note.add_run(f"The following content from {source_name} did not match any section of the template. Move it into place or remove it.")
        nr.italic = True
        last = note._p
        for pl in plan.extra:
            for b in pl.blocks:
                last, _ = w.block(last, b)

    # ---------- title and fields ----------
    for p in doc.paragraphs:
        if is_title_para(p) and plan.title:
            t = p.text.strip().lower()
            if t and len(t.split()) <= 6 and re.search(r"\b(title|name)\b", t) and not re.search(r"[\[{<]", t):
                for r in p.runs[1:]:
                    r.text = ""
                if p.runs:
                    p.runs[0].text = plan.title
            break
    field_by_token = {f.token: f for f in info.fields}
    for p in list(_all_paragraphs(doc)):
        text = p.text
        for token, f in field_by_token.items():
            if token in text:
                fv = plan.fields.get(f.key)
                _replace_token(p, token, fv.value if fv else None, f.name)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def build_preview(info: TemplateInfo, plan: MergePlan, source_title: str) -> dict:
    """What the merged document contains, for the on-screen review."""
    return {
        "title": plan.title or source_title,
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "level": s.level,
                "status": plan.status.get(s.key),
                "guidance": s.guidance,
                "fixed": s.fixed,
                "placements": [
                    {"source_title": p.source_title, "method": p.method, "note": p.note, "score": p.score, "blocks": [b.to_json() for b in p.blocks]}
                    for p in plan.placements.get(s.key, [])
                ],
            }
            for s in info.sections
        ],
        "fields": [
            {"key": f.key, "name": f.name, "token": f.token, "value": plan.fields[f.key].value if f.key in plan.fields else None,
             "method": plan.fields[f.key].method if f.key in plan.fields else None, "note": plan.fields[f.key].note if f.key in plan.fields else ""}
            for f in info.fields
        ],
        "extra": [{"source_title": p.source_title, "blocks": [b.to_json() for b in p.blocks]} for p in plan.extra],
    }


def build_report(info: TemplateInfo, plan: MergePlan) -> dict:
    st = list(plan.status.values())
    by_method = {"heading": 0, "ai": 0}
    for pls in plan.placements.values():
        for p in pls:
            by_method[p.method] = by_method.get(p.method, 0) + 1
    return {
        "sections_total": len(info.sections),
        "sections_filled": st.count("filled") + st.count("covered"),
        "sections_missing": st.count("missing"),
        "sections_template_text": st.count("template_text"),
        "placed_by_heading": by_method.get("heading", 0),
        "placed_by_ai": by_method.get("ai", 0),
        "fields_total": len(info.fields),
        "fields_filled": len(plan.fields),
        "extra_pieces": len(plan.extra),
        "missing_sections": [s.title for s in info.sections if plan.status.get(s.key) == "missing"],
        "missing_fields": [f.name for f in info.fields if f.key not in plan.fields],
        "ai": plan.ai,
        "ai_error": plan.ai_error,
        "notes": plan.notes,
    }
