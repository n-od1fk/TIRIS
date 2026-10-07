import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './client';

type Source = { id: string; name: string; kind: string; url: string; enabled: boolean;
  last_success_at: string | null; last_error: string | null };
type Job = { id: string; source_id: string; status: string; error: string | null };
type SourceType = { kind: string; label: string; hint: string; placeholder: string };
const statuses: Record<string, string> = { queued: '等待同步', running: '同步中', retrying: '稍后重试', succeeded: '同步完成', failed: '同步失败' };
const pendingJob = (job: Job) => ['queued', 'running', 'retrying'].includes(job.status);

export function SourcesPanel({ active, onChanged }: { active: boolean; onChanged: () => void }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [types, setTypes] = useState<SourceType[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [name, setName] = useState('');
  const [kind, setKind] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const previous = useRef(new Map<string, string>());
  const alive = useRef(true);
  const loadingRef = useRef(false);
  useEffect(() => () => { alive.current = false; }, []);
  const fail = (e: unknown) => { if (alive.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '来源服务暂不可用'); };
  async function load() {
    if (loadingRef.current) return;
    loadingRef.current = true; setLoading(true);
    try {
      const [nextSources, nextJobs, nextTypes] = await Promise.all([api<Source[]>('/sources'), api<Job[]>('/jobs'), api<SourceType[]>('/source-types')]);
      if (!alive.current) return;
      if (nextJobs.some(job => ['queued', 'running', 'retrying'].includes(previous.current.get(job.id) || '') && !pendingJob(job))) onChanged();
      previous.current = new Map(nextJobs.map(job => [job.id, job.status]));
      setSources(nextSources); setJobs(nextJobs); setTypes(nextTypes);
      setKind(selected => nextTypes.some(row => row.kind === selected) ? selected : nextTypes[0]?.kind || '');
      setError('');
    } catch (e) { fail(e); }
    finally { loadingRef.current = false; if (alive.current) setLoading(false); }
  }
  useEffect(() => { if (active) load(); }, [active]);
  const pending = jobs.some(pendingJob);
  useEffect(() => { if (!active || !pending) return; const timer = setInterval(load, 3000); return () => clearInterval(timer); }, [active, pending]);
  async function action(key: string, operation: () => Promise<void>) {
    setBusy(key); setError(''); setNotice('');
    try { await operation(); await load(); }
    catch (e) { fail(e); }
    finally { if (alive.current) setBusy(''); }
  }
  async function add(event: FormEvent) {
    event.preventDefault();
    await action('add', async () => {
      await api<Source>('/sources', { method: 'POST', body: JSON.stringify({ name: name.trim(), kind, url: url.trim() }) });
      if (!alive.current) return;
      setName(''); setUrl(''); setNotice('已添加到当前身份，点击同步获取内容。'); onChanged();
    });
  }
  const selectedType = types.find(row => row.kind === kind);
  return <div className="sources-view">
    <div className="section-heading"><h1>来源</h1><button className="subtle" disabled={loading || !!busy} onClick={load}>刷新</button></div>
    {error && <div className="message error" role="alert">{error}</div>}{notice && <p className="saved-notice" role="status">{notice}</p>}
    <form className="settings-panel" onSubmit={add}><h2>添加来源</h2><fieldset disabled={!!busy || !selectedType}>
      <div className="source-fields"><label>来源名称<input aria-label="来源名称" required maxLength={200} value={name} onChange={e => setName(e.target.value)} placeholder="例如：Black Hat" /></label>
        <label>来源类型<select aria-label="来源类型" value={kind} onChange={e => setKind(e.target.value)}>{!types.length && <option value="">加载中…</option>}{types.map(row => <option key={row.kind} value={row.kind}>{row.label}</option>)}</select></label></div>
      <label>来源地址<input aria-label="来源地址" required type="url" maxLength={2000} value={url} onChange={e => setUrl(e.target.value)} placeholder={selectedType?.placeholder || 'https://…'} /></label>
      <p className="scope">{selectedType?.hint}</p><button type="submit">{busy === 'add' ? '添加中…' : '添加来源'}</button>
    </fieldset></form>
    {sources.map(source => {
      const job = jobs.find(row => row.source_id === source.id);
      const running = job && pendingJob(job);
      const problem = running ? null : job?.error || source.last_error;
      return <article className="item source-card" key={source.id}>
        <div className="section-heading"><h2>{source.name}</h2><span>{source.enabled ? '已启用' : '已停用'} · {types.find(row => row.kind === source.kind)?.label || source.kind}</span></div>
        <a className="source-url" href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a>
        <p className="scope">{job ? statuses[job.status] || job.status : '尚未同步'}{source.last_success_at && ` · 最近成功 ${new Date(source.last_success_at).toLocaleString('zh-CN')}`}</p>
        {problem && <p className="failure">{problem}</p>}
        <div className="source-actions"><button disabled={!!busy || !source.enabled || !!running} onClick={() => action(source.id, async () => {
          const submitted = await api<Job>('/sources/sync', { method: 'POST', body: JSON.stringify({ source_id: source.id }) });
          if (!alive.current) return;
          previous.current.set(submitted.id, submitted.status); setJobs(rows => [submitted, ...rows.filter(row => row.id !== submitted.id)]); setNotice('已提交同步。');
        })}>{running ? statuses[job.status] : '同步'}</button>
        <button className="subtle" disabled={!!busy} onClick={() => action(source.id, async () => {
          await api(`/sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled }) }); onChanged();
        })}>{source.enabled ? '停用' : '启用'}</button></div>
      </article>;
    })}
    {!sources.length && <p className="empty">{loading ? '加载中…' : '当前身份还没有来源。'}</p>}
  </div>;
}
