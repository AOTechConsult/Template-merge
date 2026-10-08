export interface Run { text: string; bold: boolean; italic: boolean; underline: boolean }
export interface Block { kind: 'heading' | 'para' | 'list' | 'table'; runs: Run[]; level: number; ordered: boolean; rows: Run[][][] }
export interface Provider { key: string; name: string }
export interface Me {
  user: { id: string; upn: string; name: string } | null;
  demo: boolean; orgName: string; providers: Provider[]; defaultProvider: string | null; confluence: boolean; maxUploadMb: number;
}
export interface TemplateSummary { id: string; name: string; filename: string; uploaded_by: string; uploaded_at: string; size: number; sections: number; fields: number }
export interface TSection { key: string; title: string; level: number; parent: string | null; path: string[]; guidance: string; fixed: string }
export interface TField { key: string; name: string; token: string; where: string }
export interface TemplateDetail extends Omit<TemplateSummary, 'sections' | 'fields'> {
  info: { title_placeholder: boolean; sections: TSection[]; fields: TField[]; notes: string[] };
}
export interface JobSummary {
  id: string; template_id: string; template_name: string; source_name: string; source_kind: 'file' | 'confluence'; provider: string | null;
  status: 'running' | 'complete' | 'error'; error: string | null; created_by: string; created_at: string; finished_at: string | null; title: string | null;
  summary?: { sections_total: number; sections_filled: number; sections_missing: number; placed_by_ai: number; fields_total: number; fields_filled: number; extra_pieces: number };
}
export interface Placement { source_title: string; method: 'heading' | 'ai' | 'nested'; note: string; score: number; blocks: Block[] }
export interface Report {
  sections_total: number; sections_filled: number; sections_missing: number; sections_template_text: number;
  placed_by_heading: number; placed_by_ai: number; fields_total: number; fields_filled: number; extra_pieces: number;
  missing_sections: string[]; missing_fields: string[];
  ai: { provider: string; model: string; calls: number; chars_sent: number; pieces_sent: number } | null; ai_error: string; notes: string[];
}
export interface Preview {
  title: string;
  sections: { key: string; title: string; level: number; status: 'filled' | 'covered' | 'template_text' | 'missing'; guidance: string; fixed: string; placements: Placement[] }[];
  fields: { key: string; name: string; token: string; value: string | null; method: string | null; note: string }[];
  extra: { source_title: string; blocks: Block[] }[];
}
export interface JobDetail extends JobSummary { report: Report | null; preview: Preview | null }

async function req<T>(method: string, url: string, body?: FormData): Promise<T> {
  const r = await fetch(url, { method, headers: { 'X-TM': '1' }, body });
  const j = await r.json().catch(() => null);
  if (!r.ok) throw new Error(j?.error ?? `Request failed (${r.status})`);
  return j as T;
}

const form = (o: Record<string, string | undefined>) => {
  const f = new FormData();
  for (const [k, v] of Object.entries(o)) if (v !== undefined) f.append(k, v);
  return f;
};

export const api = {
  me: () => req<Me>('GET', '/api/me'),
  logout: () => req('POST', '/api/logout'),
  templates: () => req<TemplateSummary[]>('GET', '/api/templates'),
  template: (id: string) => req<TemplateDetail>('GET', `/api/templates/${id}`),
  uploadTemplate: (file: File, name: string) => {
    const f = form({ name });
    f.append('file', file);
    return req<TemplateDetail>('POST', '/api/templates', f);
  },
  deleteTemplate: (id: string) => req('DELETE', `/api/templates/${id}`),
  jobs: () => req<JobSummary[]>('GET', '/api/jobs'),
  job: (id: string) => req<JobDetail>('GET', `/api/jobs/${id}`),
  deleteJob: (id: string) => req('DELETE', `/api/jobs/${id}`),
  start: (templateId: string, matching: string, provider: string, files: File[], confluence: string) => {
    const f = form({ template_id: templateId, matching, provider, confluence });
    files.forEach((x) => f.append('files', x));
    return req<{ started: string[] }>('POST', '/api/jobs', f);
  },
  samples: (templateId: string, matching: string, provider: string) =>
    req<{ started: string[] }>('POST', '/api/demo/samples', form({ template_id: templateId, matching, provider })),
  rerun: (id: string, templateId: string, matching: string, provider: string) =>
    req<{ started: string[] }>('POST', `/api/jobs/${id}/rerun`, form({ template_id: templateId, matching, provider })),
};

export const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
export const shortSource = (j: { source_kind: string; source_name: string; title: string | null }) =>
  j.title || (j.source_kind === 'confluence' ? 'Confluence page' : j.source_name);
