# Template Merge

Rebuilds any document in your standard Word template. Upload a template once, then merge Word files,
PDFs or Confluence pages into it. Each section of the document is placed under the matching template
heading, the template's styles, headers, footers and fixed wording are kept, and anything the template
asks for that the document doesn't have is flagged in **bold red on yellow** with a Word comment.

## How a merge works

1. **The template is read.** Every paragraph in a Heading style is a section. Under each heading,
   text in *italics*, highlighted, grey, or in [square brackets] is treated as instructions and
   replaced by the document's content; any other wording is kept as written. Short placeholders such
   as `[Policy Owner]`, `{{Effective Date}}` or `<<Version>>` are fields, filled anywhere they appear,
   including tables, headers and footers.
2. **The document is read.** Word headings come from styles (or bold short lines when a document has
   none). PDF headings are inferred from font size and weight. Confluence pages use their h1 to h6
   headings, tables, lists and panels.
3. **Headings are matched.** Numbering, punctuation, plurals and common synonyms are ignored, so
   "3. Who This Applies To" fills "2. Scope" and "Passwords" fills "Password Requirements". A
   sub-section with no match of its own travels with its parent section.
4. **AI fallback (optional).** Only the pieces headings could not place, plus any unfilled fields, are
   sent to Claude or Azure OpenAI. Anything the AI places carries a "Please confirm" comment in the
   Word document and a blue marker in the on-screen preview.
5. **Nothing is dropped.** Content that still fits nowhere goes into a final "Content not in the
   template" section for a person to move or delete.

Fields are filled from the document's title and from "Label: value" lines or two-column tables in
the document (for example "Document Owner: Jordan Reyes" fills `[Policy Owner]`).

What gets flagged in the output:

| Situation | In the Word document |
|---|---|
| Template section with no content in the document | "Missing from the source document" in bold red on yellow, the template's instructions kept in bold, and a comment on the heading |
| Field not found | `[MISSING: Field name]` in bold red on yellow |
| Section with only fixed template wording | Wording kept, with a comment asking someone to confirm it applies |
| Placed by AI | Content inserted, with a comment naming the source section and the model's confidence |

## Install on the security tool's server

Template Merge runs beside the Cyber Security Response Tool on the same Ubuntu server, on its own
port (8443 by default). It reuses the same DNS name and, when it finds the security tool installed,
the same certificate authority, so browsers that already trust the security tool trust this too.

Put `install.sh` next to `template-merge.zip` on the server and run:

```bash
sudo bash install.sh            # unpacks to /opt/tmerge, starts in demo mode with sample documents
sudo bash install.sh --live     # sign-in, AI and Confluence details; checks each before switching
sudo bash install.sh --backup   # copies templates and merges to /opt/tmerge/backups
sudo bash install.sh --status
```

Open port 8443 to your admin network at your firewall. It is safe to re-run the installer; it keeps
`.env` and the data volume.

## Going live

### Entra setup (sign-in)

1. Entra admin center > App registrations > New registration, single tenant.
   Redirect URI, type **Web**: `https://<your-host>:8443/auth/callback`
2. Certificates & secrets > New client secret. Copy the **Value**.
3. API permissions: the default **User.Read** is enough (sign-in only reads your name). No other
   Microsoft permissions are needed.
4. To limit who can use it, either:
   - **Recommended:** Enterprise applications > this app > Properties > *Assignment required* = Yes,
     then assign your group under Users and groups. No settings needed in the app.
   - Or set `ENTRA_ALLOWED_GROUP_ID` and add a **groups** claim to the ID token (Token configuration >
     Add groups claim > Security groups).

### AI providers (optional, either or both)

- **Claude:** an Anthropic API key. `ANTHROPIC_MODEL` defaults to `claude-sonnet-5-5`.
- **Azure OpenAI:** the resource endpoint (`https://<name>.openai.azure.com`), a chat model
  deployment name, and an API key. Requests stay within your Azure tenant.

When both are set up, users choose per merge; `DEFAULT_AI_PROVIDER` picks the default. With neither,
the app runs on heading matching only and the AI option is greyed out.

What is sent: the template's section titles and instructions, the text of the unplaced pieces (each
cut to about 900 characters), and, only when fields are still empty, up to 20,000 characters of the
document so the model can find values like an owner or a date. Each result shows how many pieces and
characters were sent. Check your organisation's data-handling rules for the provider you enable.

### Confluence (optional)

- **Cloud:** `CONFLUENCE_BASE_URL=https://<org>.atlassian.net/wiki`, plus a service account's email
  and an API token (id.atlassian.com > Security > API tokens). The account needs view access to the
  pages people will merge.
- **Data Center / Server:** the site URL and a personal access token in `CONFLUENCE_PAT`.

Paste page links one per line. Links with a page ID (`/pages/12345/...` or `?pageId=12345`) and
`/display/SPACE/Title` links both work.

## Things to know

- **Not carried over from documents:** images, embedded objects, footnotes, tracked changes and
  comments. Text, bold, italic, underline, lists and tables are kept. Images in the template are kept.
- **PDFs** must contain text. Scanned PDFs need OCR first. Heading detection relies on fonts, so check
  the preview for PDFs with unusual formatting.
- **Older Word files** (.doc) and templates (.dotx) need saving as .docx first.
- **Lists** use the template's List Bullet / List Number styles when it has them; otherwise they are
  written as indented bullets.
- **Stored data:** templates, uploaded documents (kept so a merge can be run again) and merged
  outputs live in one SQLite file in the `tmerge-data` volume. Deleting a merge or template removes
  it. Back the volume up with `--backup`.
- Sign-in sessions are held in memory, so a restart signs everyone out.

## Tested, and not yet tested

Tested: the merge engine on the bundled Word, PDF and Confluence samples (`server/tests`), the Word
output against the Word schema (including comments), the web app end to end in demo mode, and the
installer's install and go-live paths with stand-ins for Docker and the network. The Claude and Azure
OpenAI request and reply handling is tested against simulated replies.

**Not yet tested against live services:** Entra sign-in, the real Claude and Azure OpenAI APIs, and a
real Confluence site. Try each with one document first.

## Development

```bash
cd server && pip install -r requirements.txt
DEMO_MODE=true python -m tests.test_engine      # engine tests
DEMO_MODE=true uvicorn app.main:app --reload --port 8080
cd web && npm install && npm run dev            # http://localhost:5173, proxies to :8080
```

```
server/app/readers/   Word, PDF and Confluence readers -> blocks
server/app/template.py  finds sections, instructions, fixed wording and fields in a template
server/app/matcher.py   heading matching, sub-section alignment, AI fallback, field values
server/app/writer.py    writes the output into a copy of the template, flags and comments
server/app/ai.py        Claude and Azure OpenAI clients
web/src               React interface
```
