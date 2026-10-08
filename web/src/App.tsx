import { useCallback, useEffect, useRef, useState } from 'react';
import { api, shortSource, when, type JobSummary, type Me, type TemplateDetail, type TemplateSummary } from './api';
import { ResultPage } from './Result';

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || '#/');
  useEffect(() => {
    const on = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return hash;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState('');
  const hash = useHash();
  useEffect(() => void api.me().then(setMe).catch((e) => setError(e.message)), []);

  if (error) return <div className="signin"><p className="error-box">{error}</p></div>;
  if (!me) return null;
  if (!me.user) return <SignIn me={me} />;

  const job = /^#\/result\/(\w+)/.exec(hash)?.[1];
  const tpl = /^#\/templates\/(\w+)/.exec(hash)?.[1];
  const onTemplates = hash.startsWith('#/templates');
  return (
    <>
      {me.demo && <div className="demo-bar">Demo mode. The sample template and documents are invented; nothing is sent to an AI provider.</div>}
      <header className="top">
        <a className="brand" href="#/">Template Merge <span>{me.orgName}</span></a>
        <nav>
          <a href="#/" aria-current={!onTemplates ? 'page' : undefined}>Merge</a>
          <a href="#/templates" aria-current={onTemplates ? 'page' : undefined}>Templates</a>
        </nav>
        <div className="who">
          <span className="user" title={me.user.upn}>{me.user.name || me.user.upn}</span>
          <button className="link" onClick={() => api.logout().then(() => window.location.assign('/'))}>Sign out</button>
        </div>
      </header>
      <main>
        {job ? <ResultPage key={job} id={job} me={me} /> : tpl ? <TemplatePage key={tpl} id={tpl} /> : onTemplates ? <TemplatesPage /> : <MergePage me={me} />}
      </main>
    </>
  );
}

function SignIn({ me }: { me: Me }) {
  const err = new URLSearchParams(window.location.search).get('signin_error');
  return (
    <div className="signin">
      <h1>Template Merge</h1>
      <p>Put any document into your standard template. Sections are matched by heading, the template's styles are kept, and anything the template asks for that the document doesn't have is flagged in bold.</p>
      {err && <p className="error-box">{err}</p>}
      <a className="btn primary" href="/auth/login">{me.demo ? 'Open the demo' : 'Sign in with Microsoft'}</a>
    </div>
  );
}

// ---------------- merge ----------------

function MergePage({ me }: { me: Me }) {
  const [templates, setTemplates] = useState<TemplateSummary[] | null>(null);
  const [templateId, setTemplateId] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [links, setLinks] = useState('');
  const [matching, setMatching] = useState<'headings' | 'ai'>(me.providers.length ? 'ai' : 'headings');
  const [provider, setProvider] = useState(me.defaultProvider ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.templates().then((t) => {
      setTemplates(t);
      setTemplateId((cur) => cur || t[0]?.id || '');
    }).catch((e) => setError(e.message));
  }, []);

  const add = (list: FileList | null) => {
    if (!list) return;
    const ok = Array.from(list).filter((f) => /\.(docx|pdf)$/i.test(f.name));
    const bad = Array.from(list).filter((f) => !ok.includes(f));
    setError(bad.length ? `Skipped ${bad.map((f) => f.name).join(', ')}: only Word (.docx) and PDF files can be merged.` : '');
    setFiles((cur) => [...cur, ...ok.filter((f) => !cur.some((c) => c.name === f.name && c.size === f.size))]);
  };
  const linkCount = links.split(/[\n,]+/).filter((l) => l.trim()).length;
  const count = files.length + linkCount;

  const go = async (samples = false) => {
    setBusy(true);
    setError('');
    try {
      const r = samples ? await api.samples(templateId, matching, provider) : await api.start(templateId, matching, provider, files, links);
      if (r.started.length === 1) window.location.hash = `#/result/${r.started[0]}`;
      else {
        setFiles([]);
        setLinks('');
        window.dispatchEvent(new Event('jobs-changed'));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (templates && !templates.length) {
    return (
      <div className="page narrow">
        <h1>Merge documents into a template</h1>
        <div className="empty">
          <h2>Start with a template</h2>
          <p>Upload the Word document that every merged document should look like. Section titles need to use Heading styles.</p>
          <a className="btn primary" href="#/templates">Upload a template</a>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="merge-grid">
        <section className="panel">
          <h1>Merge documents into a template</h1>
          <p className="muted lead">Each document is rebuilt in the template's layout. Sections the template expects but the document lacks are flagged in bold red.</p>

          <div className="step">
            <h2><span className="num">1</span>Template</h2>
            <select value={templateId} onChange={(e) => setTemplateId(e.target.value)} aria-label="Template">
              {(templates ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} ({t.sections} sections)</option>)}
            </select>
            <p className="small"><a href={`#/templates/${templateId}`}>See what this template expects</a> or <a href="#/templates">upload another</a></p>
          </div>

          <div className="step">
            <h2><span className="num">2</span>Documents</h2>
            <div
              className={`drop ${drag ? 'over' : ''}`}
              onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
              onClick={() => input.current?.click()}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
            >
              <strong>Drop Word or PDF files here</strong>
              <span className="muted">or click to choose. Up to {me.maxUploadMb} MB each.</span>
              <input ref={input} type="file" multiple accept=".docx,.pdf" hidden onChange={(e) => { add(e.target.files); e.target.value = ''; }} />
            </div>
            {files.length > 0 && (
              <ul className="files">
                {files.map((f, i) => (
                  <li key={f.name + i}>
                    <span className="ext">{f.name.split('.').pop()?.toUpperCase()}</span>
                    <span className="fname">{f.name}</span>
                    <span className="muted small">{(f.size / 1024).toFixed(0)} KB</span>
                    <button className="link" onClick={() => setFiles(files.filter((_, j) => j !== i))}>Remove</button>
                  </li>
                ))}
              </ul>
            )}
            {me.confluence && (
              <label className="field"><span>Confluence pages <span className="muted">(one link per line)</span></span>
                <textarea rows={3} value={links} onChange={(e) => setLinks(e.target.value)} placeholder="https://yourorg.atlassian.net/wiki/spaces/IT/pages/12345/Access+Policy" />
              </label>
            )}
          </div>

          <div className="step">
            <h2><span className="num">3</span>Matching</h2>
            <label className="choice">
              <input type="radio" checked={matching === 'headings'} onChange={() => setMatching('headings')} />
              <span><strong>Headings only</strong><span className="muted">Nothing leaves the server. Content under headings that don't resemble the template's is listed at the end.</span></span>
            </label>
            <label className={`choice ${!me.providers.length ? 'disabled' : ''}`}>
              <input type="radio" checked={matching === 'ai'} disabled={!me.providers.length} onChange={() => setMatching('ai')} />
              <span>
                <strong>Headings, then AI for what's left</strong>
                <span className="muted">
                  {me.providers.length
                    ? 'Only content the headings could not place is sent to the AI provider. Anything the AI places is marked for review.'
                    : 'No AI provider is set up on the server.'}
                </span>
              </span>
            </label>
            {matching === 'ai' && me.providers.length > 1 && (
              <select className="provider" value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="AI provider">
                {me.providers.map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}
              </select>
            )}
          </div>

          {error && <p className="error-box">{error}</p>}
          <div className="actions">
            <button className="btn primary" disabled={busy || !templateId || !count} onClick={() => go()}>
              {busy ? 'Starting' : count > 1 ? `Merge ${count} documents` : 'Merge document'}
            </button>
            {me.demo && <button className="btn" disabled={busy || !templateId} onClick={() => go(true)}>Try the sample documents</button>}
          </div>
        </section>
        <RecentJobs />
      </div>
    </div>
  );
}

function RecentJobs() {
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const load = useCallback(() => api.jobs().then(setJobs).catch(() => setJobs([])), []);
  useEffect(() => {
    void load();
    window.addEventListener('jobs-changed', load);
    return () => window.removeEventListener('jobs-changed', load);
  }, [load]);
  const running = jobs?.some((j) => j.status === 'running');
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(load, 1500);
    return () => window.clearInterval(t);
  }, [running, load]);

  return (
    <section className="recent">
      <h2>Recent merges</h2>
      {jobs && !jobs.length && <p className="muted">Merged documents appear here.</p>}
      <ul className="jobs">
        {(jobs ?? []).map((j) => (
          <li key={j.id}>
            <a href={`#/result/${j.id}`} className={`job ${j.status}`}>
              <span className="job-title">{shortSource(j)}</span>
              <span className="muted small">{j.template_name} · {when(j.created_at)}</span>
              <span className="job-state">
                {j.status === 'running' && <span className="st running">Merging</span>}
                {j.status === 'error' && <span className="st error">Failed</span>}
                {j.summary && (
                  <>
                    <span className="st">{j.summary.sections_filled} of {j.summary.sections_total} sections</span>
                    {j.summary.sections_missing > 0 ? <span className="st missing">{j.summary.sections_missing} missing</span> : <span className="st ok">Complete</span>}
                  </>
                )}
              </span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------- templates ----------------

function TemplatesPage() {
  const [list, setList] = useState<TemplateSummary[] | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => void api.templates().then(setList).catch((e) => setError(e.message)), []);

  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError('');
    try {
      const t = await api.uploadTemplate(file, name);
      window.location.hash = `#/templates/${t.id}`;
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <h1>Templates</h1>
      <div className="tpl-grid">
        <div>
          {list && !list.length && <p className="muted">No templates yet. Upload one to start merging.</p>}
          <ul className="tpl-list">
            {(list ?? []).map((t) => (
              <li key={t.id}>
                <a href={`#/templates/${t.id}`}>
                  <strong>{t.name}</strong>
                  <span className="muted small">{t.sections} sections, {t.fields} fields · uploaded {when(t.uploaded_at)} by {t.uploaded_by}</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
        <form className="panel upload" onSubmit={upload}>
          <h2>Upload a template</h2>
          <p className="muted small">A Word (.docx) file laid out the way every merged document should look.</p>
          <ul className="howto small">
            <li>Put each section title in a <strong>Heading</strong> style.</li>
            <li>Write instructions in <em>italics</em> or [brackets]; they're replaced by the document's content. Other wording under a heading is kept.</li>
            <li>Use short placeholders like [Policy Owner] or {'{{Effective Date}}'} for details to fill in, including in headers and footers.</li>
          </ul>
          <label className="field">File<input type="file" accept=".docx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required /></label>
          <label className="field"><span>Name <span className="muted">(optional)</span></span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Information Security Policy" /></label>
          {error && <p className="error-box">{error}</p>}
          <button className="btn primary" disabled={!file || busy}>{busy ? 'Reading template' : 'Upload template'}</button>
        </form>
      </div>
    </div>
  );
}

function TemplatePage({ id }: { id: string }) {
  const [t, setT] = useState<TemplateDetail | null>(null);
  const [error, setError] = useState('');
  useEffect(() => void api.template(id).then(setT).catch((e) => setError(e.message)), [id]);
  if (error) return <div className="page"><p className="error-box">{error}</p></div>;
  if (!t) return null;
  const remove = async () => {
    if (!window.confirm(`Delete the template "${t.name}"? Merges already made with it are kept.`)) return;
    await api.deleteTemplate(id);
    window.location.hash = '#/templates';
  };
  return (
    <div className="page narrow-wide">
      <a className="back" href="#/templates">All templates</a>
      <div className="page-head">
        <div>
          <h1>{t.name}</h1>
          <p className="muted">{t.filename}, uploaded {when(t.uploaded_at)} by {t.uploaded_by}</p>
        </div>
        <div className="actions">
          <a className="btn" href={`/api/templates/${id}/file`}>Download original</a>
          <button className="btn danger-btn" onClick={remove}>Delete</button>
        </div>
      </div>
      {t.info.notes.map((n) => <p className="warn-box" key={n}>{n}</p>)}
      <h2>Sections</h2>
      <p className="muted small">What each merged document is checked against. <em>Instructions</em> are replaced by the document's content; <em>fixed wording</em> is kept as written.</p>
      <ol className="outline">
        {t.info.sections.map((s) => (
          <li key={s.key} style={{ marginLeft: `${(s.level - 1) * 1.5}rem` }}>
            <strong>{s.title}</strong>
            {s.guidance && <p className="g"><span className="tag">Instructions</span>{s.guidance}</p>}
            {s.fixed && <p className="f"><span className="tag">Fixed wording</span>{s.fixed}</p>}
          </li>
        ))}
      </ol>
      <h2>Fields</h2>
      {!t.info.fields.length ? <p className="muted">No placeholders found.</p> : (
        <table className="grid">
          <thead><tr><th>Field</th><th>Placeholder</th><th>Where</th></tr></thead>
          <tbody>{t.info.fields.map((f) => <tr key={f.key}><td>{f.name}</td><td><code>{f.token}</code></td><td>{f.where}</td></tr>)}</tbody>
        </table>
      )}
    </div>
  );
}
