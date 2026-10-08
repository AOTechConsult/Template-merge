import { useCallback, useEffect, useState } from 'react';
import { api, shortSource, when, type Block, type JobDetail, type Me, type Run, type TemplateSummary } from './api';

function Runs({ runs }: { runs: Run[] }) {
  return (
    <>
      {runs.map((r, i) => {
        let el: React.ReactNode = r.text.split('\n').flatMap((t, k, a) => (k < a.length - 1 ? [t, <br key={k} />] : [t]));
        if (r.bold) el = <strong>{el}</strong>;
        if (r.italic) el = <em>{el}</em>;
        if (r.underline) el = <u>{el}</u>;
        return <span key={i}>{el}</span>;
      })}
    </>
  );
}

function Blocks({ blocks, base }: { blocks: Block[]; base: number }) {
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === 'heading') {
          const lvl = Math.min(Math.max(b.level, base + 1), 6);
          return <p key={i} className={`doc-h doc-h${Math.min(lvl, 4)}`}><Runs runs={b.runs} /></p>;
        }
        if (b.kind === 'list') {
          return <p key={i} className="doc-li" style={{ marginLeft: `${1.25 + b.level * 1.25}rem` }}><span className="bullet">{b.ordered ? '–' : '•'}</span><Runs runs={b.runs} /></p>;
        }
        if (b.kind === 'table') {
          return (
            <table key={i} className="doc-table">
              <tbody>
                {b.rows.map((row, ri) => (
                  <tr key={ri}>{row.map((cell, ci) => (ri === 0 ? <th key={ci}><Runs runs={cell} /></th> : <td key={ci}><Runs runs={cell} /></td>))}</tr>
                ))}
              </tbody>
            </table>
          );
        }
        return <p key={i} className="doc-p"><Runs runs={b.runs} /></p>;
      })}
    </>
  );
}

const STATUS_LABEL = { filled: 'Filled', covered: 'Covered by sub-sections', template_text: 'Template wording only', missing: 'Missing' } as const;

export function ResultPage({ id, me }: { id: string; me: Me }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [error, setError] = useState('');
  const [rerunOpen, setRerunOpen] = useState(false);
  const load = useCallback(() => api.job(id).then(setJob).catch((e) => setError(e.message)), [id]);
  useEffect(() => void load(), [load]);
  useEffect(() => {
    if (job?.status !== 'running') return;
    const t = window.setInterval(load, 1200);
    return () => window.clearInterval(t);
  }, [job?.status, load]);

  if (error) return <div className="page"><p className="error-box">{error}</p></div>;
  if (!job) return null;
  const r = job.report;
  const p = job.preview;
  const remove = async () => {
    if (!window.confirm('Delete this merge and its output document?')) return;
    await api.deleteJob(id);
    window.location.hash = '#/';
  };

  return (
    <div className="page result">
      <a className="back" href="#/">Merge more documents</a>
      <div className="page-head">
        <div>
          <h1>{shortSource(job)}</h1>
          <p className="muted">
            Merged into <a href={`#/templates/${job.template_id}`}>{job.template_name}</a>
            {job.source_kind === 'confluence' ? ' from a Confluence page' : ` from ${job.source_name}`}, {when(job.created_at)} by {job.created_by}
          </p>
        </div>
        <div className="actions">
          {job.status === 'complete' && <a className="btn primary" href={`/api/jobs/${id}/download`}>Download Word document</a>}
          {job.status !== 'running' && <button className="btn" onClick={() => setRerunOpen(!rerunOpen)}>Run again</button>}
          <button className="btn danger-btn" onClick={remove}>Delete</button>
        </div>
      </div>
      {rerunOpen && <Rerun job={job} me={me} onClose={() => setRerunOpen(false)} />}

      {job.status === 'running' && <div className="empty"><h2 className="pulse">Merging</h2><p>Reading the document and matching it to the template. This usually takes a few seconds{job.provider ? '; AI matching can take up to a minute' : ''}.</p></div>}
      {job.status === 'error' && <p className="error-box">{job.error}</p>}

      {r && p && (
        <>
          <dl className="stats">
            <div><dt>Sections found</dt><dd>{r.sections_filled} <small>of {r.sections_total}</small></dd></div>
            <div className={r.sections_missing ? 'bad' : ''}><dt>Missing</dt><dd>{r.sections_missing}</dd></div>
            <div><dt>Fields filled</dt><dd>{r.fields_filled} <small>of {r.fields_total}</small></dd></div>
            <div><dt>Placed by AI</dt><dd>{r.placed_by_ai}</dd></div>
            <div className={r.extra_pieces ? 'warn' : ''}><dt>Not in template</dt><dd>{r.extra_pieces}</dd></div>
          </dl>

          {(r.missing_sections.length > 0 || r.missing_fields.length > 0) && (
            <div className="missing-box">
              <h2>Missing from the document</h2>
              <p className="small">These are flagged in bold red in the Word document, with a comment on each.</p>
              {r.missing_sections.length > 0 && <p><strong>Sections:</strong> {r.missing_sections.join(', ')}</p>}
              {r.missing_fields.length > 0 && <p><strong>Details:</strong> {r.missing_fields.join(', ')}</p>}
            </div>
          )}
          {r.notes.map((n) => <p className="warn-box" key={n}>{n}</p>)}
          {r.ai && <p className="muted small">AI matching: {r.ai.provider} ({r.ai.model}), {r.ai.pieces_sent} piece{r.ai.pieces_sent === 1 ? '' : 's'} of content sent, {r.ai.chars_sent.toLocaleString()} characters.</p>}

          <h2 className="preview-title">Preview</h2>
          <p className="muted small">Content only; download the Word document for the template's full layout, headers and footers.</p>
          <article className="doc">
            <h1 className="doc-title">{p.title}</h1>
            {p.fields.length > 0 && (
              <table className="doc-table fields">
                <tbody>
                  {p.fields.map((f) => (
                    <tr key={f.key}>
                      <th>{f.name}</th>
                      <td>{f.value ?? <span className="flag">Missing</span>}{f.method === 'ai' && <span className="by"> AI</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {p.sections.map((s) => (
              <section key={s.key} className={`doc-sec ${s.status}`}>
                <div className={`doc-h doc-h${Math.min(s.level, 4)}`}>
                  {s.title}
                  {s.status !== 'filled' && <span className={`chip ${s.status}`}>{STATUS_LABEL[s.status]}</span>}
                </div>
                {s.fixed && s.fixed !== '[table]' && <p className="doc-p fixed">{s.fixed}</p>}
                {s.placements.map((pl, i) => (
                  <div key={i} className={`placed ${pl.method}`}>
                    <p className="from">
                      {pl.method === 'ai' ? 'Placed by AI' : 'Matched by heading'} from <q>{pl.source_title}</q>
                      {pl.note && <span>. {pl.note}</span>}
                    </p>
                    <Blocks blocks={pl.blocks} base={s.level} />
                  </div>
                ))}
                {s.status === 'missing' && (
                  <div className="flagged">
                    <p className="flag">Missing from the source document.</p>
                    {s.guidance && <p className="flag-g">{s.guidance}</p>}
                  </div>
                )}
              </section>
            ))}
            {p.extra.length > 0 && (
              <section className="doc-sec extra">
                <div className="doc-h doc-h1">Content not in the template<span className="chip template_text">Review</span></div>
                <p className="doc-p fixed">These parts of the document did not match any template section. Move them into place or remove them.</p>
                {p.extra.map((e, i) => <div key={i} className="placed"><Blocks blocks={e.blocks} base={1} /></div>)}
              </section>
            )}
          </article>
        </>
      )}
    </div>
  );
}

function Rerun({ job, me, onClose }: { job: JobDetail; me: Me; onClose: () => void }) {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [templateId, setTemplateId] = useState(job.template_id);
  const [matching, setMatching] = useState(job.provider ? 'ai' : 'headings');
  const [provider, setProvider] = useState(job.provider ?? me.defaultProvider ?? '');
  const [error, setError] = useState('');
  useEffect(() => void api.templates().then(setTemplates), []);
  const go = async () => {
    try {
      const r = await api.rerun(job.id, templateId, matching, provider);
      onClose();
      window.location.hash = `#/result/${r.started[0]}`;
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="panel rerun">
      <label className="field">Template
        <select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>{templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
      </label>
      <label className="field">Matching
        <select value={matching === 'ai' ? provider : 'headings'} onChange={(e) => { if (e.target.value === 'headings') setMatching('headings'); else { setMatching('ai'); setProvider(e.target.value); } }}>
          <option value="headings">Headings only</option>
          {me.providers.map((p) => <option key={p.key} value={p.key}>Headings, then {p.name}</option>)}
        </select>
      </label>
      {error && <p className="error-box">{error}</p>}
      <div className="actions"><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={go}>Run again</button></div>
    </div>
  );
}
