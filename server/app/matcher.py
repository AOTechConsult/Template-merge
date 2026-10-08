"""Decides where every piece of the source document goes in the template.

Order of precedence:
  1. Heading match: source headings compared with template headings (numbering, punctuation and
     common synonyms ignored).
  2. Nesting: an unmatched sub-section travels with its matched parent.
  3. AI fallback (optional): remaining pieces are offered to the chosen model.
  4. Anything still unplaced goes to a "Content not in the template" section, so nothing is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ai import AIClient, AIError
from .model import Block, Document, Run, Section, clean_heading, split_sections
from .template import TemplateInfo

STOP = {"the", "and", "of", "for", "to", "a", "an", "in", "on", "or", "with", "by", "this", "policy", "procedure", "standard", "document"}

SYNONYMS = [
    {"purpose", "objective", "objectives", "overview", "introduction", "intent", "background", "summary", "purpose and scope"},
    {"scope", "applicability", "applies to", "audience", "coverage", "who this applies to"},
    {"roles and responsibilities", "responsibilities", "roles", "accountability", "ownership"},
    {"definitions", "terms", "glossary", "terminology", "terms and definitions", "acronyms"},
    {"policy", "policy statement", "policy statements", "requirements", "policy requirements", "standards", "policy details", "statement", "rules", "guidelines", "controls"},
    {"procedure", "procedures", "process", "steps", "instructions"},
    {"exceptions", "exemptions", "exception process", "exception handling", "deviations", "waivers"},
    {"enforcement", "compliance", "violations", "sanctions", "non compliance", "noncompliance", "consequences", "disciplinary action"},
    {"references", "related documents", "related policies", "related standards", "supporting documents", "see also", "regulatory references"},
    {"revision history", "document history", "version history", "change log", "changelog", "revisions", "change history", "document control"},
    {"review", "review cycle", "policy review", "maintenance", "review and approval", "approval"},
    {"contacts", "contact information", "questions", "contact", "points of contact"},
]

TITLE_FIELD = re.compile(r"\b(title|policy name|document name|procedure name|standard name|name of policy)\b", re.I)
LABEL_LINE = re.compile(r"^\s*([A-Za-z][\w /&().'-]{1,40}?)\s*[:–-]\s+(.{1,200}?)\s*$")


def _stem(w: str) -> str:
    for suf in ("ies", "es", "s"):
        if len(w) > 4 and w.endswith(suf) and not w.endswith("ss"):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _tokens(t: str) -> set[str]:
    return {_stem(w) for w in t.split() if w not in STOP}


def _groups(t: str) -> set[int]:
    out = set()
    for i, g in enumerate(SYNONYMS):
        if t in g or any(len(p) > 4 and re.search(rf"\b{re.escape(p)}\b", t) for p in g):
            out.add(i)
    return out


def similarity(a: str, b: str) -> float:
    a, b = clean_heading(a), clean_heading(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = _groups(a), _groups(b)
    if ga & gb and len(a.split()) <= 5 and len(b.split()) <= 5:
        # Prefer exact synonym hits ("Scope" ~ "Applicability") over loose containment.
        return 0.9
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    j = len(ta & tb) / len(ta | tb)
    if ta <= tb or tb <= ta:
        j = max(j, 0.75)
    return j


@dataclass
class Placement:
    source_id: str
    source_title: str
    blocks: list[Block]
    method: str  # heading | nested | ai
    score: float = 1.0
    note: str = ""


@dataclass
class FieldValue:
    value: str
    method: str  # title | label | ai
    note: str = ""


@dataclass
class MergePlan:
    placements: dict[str, list[Placement]] = field(default_factory=dict)  # template section key -> placements
    extra: list[Placement] = field(default_factory=list)
    fields: dict[str, FieldValue] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)  # filled | covered | template_text | missing
    ai: dict | None = None
    ai_error: str = ""
    notes: list[str] = field(default_factory=list)
    title: str = ""


def _section_blocks(sec: Section, children: list[Section], base_level: int) -> list[Block]:
    """A section's own body plus nested child sections, with child headings shifted under base_level."""
    out = list(sec.body)
    for ch in children:
        lvl = min(base_level + (ch.level - sec.level if sec.level else 1), 9)
        out.append(Block("heading", runs=[Run(ch.title)], level=max(lvl, base_level + 1)))
        out.extend(ch.body)
    return out


def _label_pairs(doc: Document) -> list[tuple[str, str, int]]:
    """(label, value, block index) from 'Label: value' lines and two-column tables."""
    pairs = []
    for i, b in enumerate(doc.blocks):
        if b.kind in ("para", "list"):
            for part in re.split(r"(?<=[.;|])\s+(?=[A-Z][\w /&()-]{1,30}:)", b.text):
                m = LABEL_LINE.match(part)
                if m:
                    pairs.append((m.group(1), m.group(2).rstrip(".;"), i))
        elif b.kind == "table":
            rows = [[("".join(r.text for r in c)).strip() for c in row] for row in b.rows]
            for row in rows:
                if len(row) >= 2 and row[0] and row[1] and len(row[0]) <= 40:
                    pairs.append((row[0].rstrip(":"), row[1], i))
            # Header row of labels with a single row of values beneath.
            if len(rows) == 2 and all(rows[0]) and len(rows[0]) == len(rows[1]):
                for lab, val in zip(rows[0], rows[1]):
                    if val:
                        pairs.append((lab, val, i))
    return pairs


def build_plan(template: TemplateInfo, doc: Document, ai: AIClient | None) -> MergePlan:
    plan = MergePlan(title=doc.title)
    plan.placements = {s.key: [] for s in template.sections}
    sections = split_sections(doc)
    by_id = {s.id: s for s in sections}
    tsections = {s.key: s for s in template.sections}

    # ---------- 1. heading match ----------
    pairs = []
    for s in sections:
        if not s.title:
            continue
        for t in template.sections:
            sc = similarity(s.title, t.title)
            if sc >= 0.6:
                if s.level and s.level == t.level:
                    sc += 0.03
                pairs.append((sc, s.id, t.key))
    pairs.sort(key=lambda x: -x[0])
    matched: dict[str, str] = {}  # source id -> template key
    taken: set[str] = set()
    for sc, sid, tkey in pairs:
        if sid in matched:
            continue
        if tkey in taken and sc < 0.85:
            continue
        matched[sid] = tkey
        taken.add(tkey)

    # Sub-sections under a matched parent: look for a looser match among that template section's own sub-sections
    # ("User Access Reviews" under "Requirements" -> "Access Control" under "Policy Statements").
    def tdescendants(key: str) -> list:
        out = []
        for t in template.sections:
            p = t.parent
            while p and p != key:
                p = tsections[p].parent
            if p == key:
                out.append(t)
        return out

    changed = True
    while changed:
        changed = False
        for s in sections:
            if s.id in matched or not s.title or not s.parent:
                continue
            p = s.parent
            while p and p not in matched:
                p = by_id[p].parent
            if not p:
                continue
            best, best_sc = None, 0.0
            for t in tdescendants(matched[p]):
                if t.key in taken:
                    continue
                sc = similarity(s.title, t.title)
                shared = _tokens(clean_heading(s.title)) & _tokens(clean_heading(t.title))
                if sc >= 0.25 and shared and sc > best_sc:
                    best, best_sc = t.key, sc
            if best:
                matched[s.id] = best
                taken.add(best)
                changed = True

    def matched_ancestor(s: Section) -> str | None:
        p = s.parent
        while p:
            if p in matched:
                return p
            p = by_id[p].parent
        return None

    # Group unmatched descendants with their nearest matched ancestor, or with their top-most unmatched ancestor.
    owner: dict[str, str] = {}
    for s in sections:
        if s.id in matched or not s.title:
            continue
        anc = matched_ancestor(s)
        if anc:
            owner[s.id] = anc
        else:
            root = s
            while root.parent and root.parent not in matched:
                root = by_id[root.parent]
            if root.id != s.id:
                owner[s.id] = root.id

    def children_of(sid: str) -> list[Section]:
        return [s for s in sections if owner.get(s.id) == sid]

    for sid, tkey in matched.items():
        s = by_id[sid]
        tl = tsections[tkey].level
        plan.placements[tkey].append(
            Placement(sid, s.title, _section_blocks(s, children_of(sid), tl), "heading", round(similarity(s.title, tsections[tkey].title), 2))
        )
        for ch in children_of(sid):
            plan.placements[tkey][-1].note = (plan.placements[tkey][-1].note + "; " if plan.placements[tkey][-1].note else "") + f"includes sub-section '{ch.title}'"

    # ---------- fields (before deciding what preamble content is left over) ----------
    consumed_blocks: set[int] = set()
    label_pairs = _label_pairs(doc)
    for f in template.fields:
        if TITLE_FIELD.search(f.name) and doc.title:
            plan.fields[f.key] = FieldValue(doc.title, "title", "document title")
            continue
        best, best_sc = None, 0.0
        for lab, val, bi in label_pairs:
            sc = similarity(lab, f.name)
            if sc > best_sc:
                best, best_sc = (lab, val, bi), sc
        if best and best_sc >= 0.75:
            plan.fields[f.key] = FieldValue(best[1], "label", f"from '{best[0]}'")
            consumed_blocks.add(best[2])

    # ---------- 2/3. leftovers: unmatched top-level sections and preamble blocks ----------
    block_index = {id(b): i for i, b in enumerate(doc.blocks)}
    units: list[dict] = []
    unit_payload: dict[str, Placement] = {}
    for s in sections:
        if s.id in matched or s.id in owner:
            continue
        if not s.title:  # preamble: each block is its own piece
            for b in s.body:
                i = block_index.get(id(b))
                if i in consumed_blocks or not b.text.strip():
                    continue
                if b.kind == "table" and i is not None and any(i == bi for _, _, bi in label_pairs):
                    continue  # document-control tables feed fields; not body content
                uid = f"u{len(units) + 1}"
                units.append({"id": uid, "heading": "", "text": b.text})
                unit_payload[uid] = Placement(s.id, "(text before the first heading)", [b], "ai")
            continue
        blocks = _section_blocks(s, children_of(s.id), 1)
        uid = f"u{len(units) + 1}"
        units.append({"id": uid, "heading": s.title, "text": "\n".join(b.text for b in blocks)})
        unit_payload[uid] = Placement(s.id, s.title, blocks, "ai")

    assigned: dict[str, dict] = {}
    if ai and units:
        try:
            tdesc = [{"key": t.key, "title": t.title, "path": t.path, "guidance": t.guidance} for t in template.sections]
            for i in range(0, len(units), 60):
                assigned.update(ai.place_units(tdesc, units[i:i + 60]))
            missing_fields = [{"key": f.key, "name": f.name} for f in template.fields if f.key not in plan.fields]
            if missing_fields:
                found = ai.extract_fields(missing_fields, "\n".join(b.text for b in doc.blocks))
                for k, v in found.items():
                    plan.fields[k] = FieldValue(v["value"], "ai", f'"{v["quote"]}"' if v["quote"] else "")
        except AIError as e:
            plan.ai_error = str(e)
            plan.notes.append(f"AI matching was skipped: {e} Unplaced content is listed at the end instead.")
        plan.ai = {"provider": ai.usage.provider, "model": ai.usage.model, "calls": ai.usage.calls, "chars_sent": ai.usage.chars_sent, "pieces_sent": len(units)}

    for u in units:
        p = unit_payload[u["id"]]
        a = assigned.get(u["id"])
        if a and a["section"]:
            tkey = a["section"]
            p.method = "ai"
            p.note = f'AI ({a["confidence"] or "unrated"} confidence): {a["reason"]}'.strip()
            plan.placements[tkey].append(p)
        else:
            p.method = "unplaced"
            if p.source_title and p.source_title != "(text before the first heading)":
                p.blocks = [Block("heading", runs=[Run(p.source_title)], level=2)] + p.blocks
            plan.extra.append(p)

    # Keep each template section's placements in source order.
    order = {s.id: i for i, s in enumerate(sections)}
    for k in plan.placements:
        plan.placements[k].sort(key=lambda p: order.get(p.source_id, 0))

    # ---------- status per template section ----------
    def has_filled_descendant(key: str) -> bool:
        return any(t.parent == key and (plan.placements[t.key] or has_filled_descendant(t.key)) for t in template.sections)

    for t in template.sections:
        if plan.placements[t.key]:
            plan.status[t.key] = "filled"
        elif not t.guidance and has_filled_descendant(t.key):
            plan.status[t.key] = "covered"
        elif t.fixed and not t.guidance:
            plan.status[t.key] = "template_text"
        else:
            plan.status[t.key] = "missing"
    if doc.notes:
        plan.notes.extend(doc.notes)
    return plan
