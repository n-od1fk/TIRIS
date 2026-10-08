import { useEffect, useRef, useState } from 'react';
import { api } from './client';
import { PlatformTools } from './PlatformTools';
import { SourceOnboarding } from './SourceOnboarding';
import type { Connection } from './SourceOnboarding';

type Source = Connection;
type Job = { id: string; source_id: string; status: string; error: string | null };
type SourceType = { kind: string; label: string; hint: string; placeholder: string; platform_tools?: boolean };
const statuses: Record<string, string> = { queued: '等待同步', running: '同步中', retrying: '稍后重试', succeeded: '同步完成', failed: '同步失败' };
const pendingJob = (job: Job) => ['queued', 'running', 'retrying'].includes(job.status);

export function SourcesPanel({ active, onChanged }: { active: boolean; onChanged: () => void }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [types, setTypes] = useState<SourceType[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [editing, setEditing] = useState<Source | null>(null);
  const [formVersion, setFormVersion] = useState(0);
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [opened, setOpened] = useState('');
  const [confirmDelete, setConfirmDelete] = useState('');
  const [removed, setRemoved] = useState<Source | null>(null);
  const previous = useRef(new Map<string, string>());
  const alive = useRef(true);
  const loadingRef = useRef(false);
  const loadVersion = useRef(0);
  useEffect(() => () => { alive.current = false; }, []);
  const fail = (e: unknown) => { if (alive.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '来源服务暂不可用'); };
  async function load(force = false) {
    if (loadingRef.current && !force) return;
    const ticket = ++loadVersion.current;
    loadingRef.current = true; setLoading(true);
    try {
      const [nextSources, nextJobs, nextTypes] = await Promise.all([api<Source[]>('/sources'), api<Job[]>('/jobs'), api<SourceType[]>('/source-types')]);
      if (!alive.current || ticket !== loadVersion.current) return;
      if (nextJobs.some(job => ['queued', 'running', 'retrying'].includes(previous.current.get(job.id) || '') && !pendingJob(job))) onChanged();
      previous.current = new Map(nextJobs.map(job => [job.id, job.status]));
      setSources(nextSources); setJobs(nextJobs); setTypes(nextTypes);
      setError('');
    } catch (e) { if (ticket === loadVersion.current) fail(e); }
    finally { if (ticket === loadVersion.current) { loadingRef.current = false; if (alive.current) setLoading(false); } }
  }
  useEffect(() => { if (active) load(); }, [active]);
  const pending = jobs.some(pendingJob);
  useEffect(() => { if (!active || !pending) return; const timer = setInterval(load, 3000); return () => clearInterval(timer); }, [active, pending]);
  async function action(key: string, operation: () => Promise<void>) {
    setBusy(key); setError(''); setNotice('');
    try { await operation(); await load(true); }
    catch (e) { fail(e); }
    finally { if (alive.current) setBusy(''); }
  }
  async function remove(source: Source) {
    await action(source.id, async () => {
      await api(`/sources/${source.id}`, { method: 'DELETE' });
      if (!alive.current) return;
      setRemoved(source); setConfirmDelete(''); setOpened('');
      if (editing?.id === source.id) { setEditing(null); setFormVersion(n => n + 1); }
      setSources(rows => rows.filter(row => row.id !== source.id));
      setNotice(`已删除“${source.name}”，停止后续采集，已收藏资料与历史保留。`); onChanged();
    });
  }
  return <div className="sources-view">
    <div className="section-heading"><h1>来源</h1><button className="subtle" disabled={loading || !!busy} onClick={() => load()}>刷新</button></div>
    {error && <div className="message error" role="alert">{error}</div>}{notice && <p className="saved-notice" role="status">{notice}</p>}
    {removed && <button className="subtle" disabled={!!busy} onClick={() => action('restore', async () => {
      await api('/sources', { method: 'POST', body: JSON.stringify({ name: removed.name, kind: removed.kind, url: removed.url }) });
      if (!alive.current) return;
      setRemoved(null); setNotice('已恢复来源，可继续使用。'); onChanged();
    })}>撤销删除</button>}
    <SourceOnboarding key={formVersion} editing={editing} onCancel={() => { setEditing(null); setFormVersion(n => n + 1); }} onSaved={async message => {
      if (!alive.current) return;
      setEditing(null); setFormVersion(n => n + 1); setRemoved(null); setNotice(message); onChanged(); await load(true);
    }} />
    {sources.map(source => {
      const job = jobs.find(row => row.source_id === source.id);
      const running = job && pendingJob(job);
      const problem = running ? null : job?.error || source.last_error;
      const platformOnly = source.sync_supported === false || source.sync_supported === undefined && (['arxiv', 'web'].includes(source.kind) || source.kind === 'github' && new URL(source.url).pathname === '/');
      const hasTools = types.find(row => row.kind === source.kind)?.platform_tools;
      return <article className="item source-card" key={source.id}>
        <div className="section-heading"><h2>{source.name}</h2><span>{source.enabled ? '已启用' : '已停用'} · {source.access_type || types.find(row => row.kind === source.kind)?.label || source.kind}</span></div>
        {source.input_url && source.input_url !== source.url && <p className="scope">输入地址：{source.input_url}</p>}
        {source.access_type === 'platform_api' && <p className="scope">平台 API 连接 · {source.credential_configured ? '已配置凭证' : '匿名公开读取'} · 不绑定单个仓库</p>}
        <a className="source-url" href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a>
        <p className="scope">{job ? statuses[job.status] || job.status : '尚未同步'}{source.last_success_at && ` · 最近成功 ${new Date(source.last_success_at).toLocaleString('zh-CN')}`}</p>
        {problem && <p className="failure">{problem}</p>}
        <div className="source-actions">{hasTools && <button className="subtle" disabled={!source.enabled} onClick={() => setOpened(opened === source.id ? '' : source.id)}>{opened === source.id ? '收起平台工具' : '平台工具'}</button>}
        {!platformOnly && <button disabled={!!busy || !source.enabled || !!running} onClick={() => action(source.id, async () => {
          const submitted = await api<Job>('/sources/sync', { method: 'POST', body: JSON.stringify({ source_id: source.id }) });
          if (!alive.current) return;
          previous.current.set(submitted.id, submitted.status); setJobs(rows => [submitted, ...rows.filter(row => row.id !== submitted.id)]); setNotice('已提交同步。');
        })}>{running ? statuses[job.status] : '同步'}</button>}
        <button className="subtle" disabled={!!busy} onClick={() => action(source.id, async () => {
          await api(`/sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled }) }); onChanged();
        })}>{source.enabled ? '停用' : '启用'}</button>
        <button className="subtle danger" disabled={!!busy} onClick={() => setConfirmDelete(source.id)}>删除来源</button></div>
        <button className="subtle" disabled={!!busy || !!running} onClick={() => { setEditing(source); setFormVersion(n => n + 1); }}>编辑来源</button>
        {confirmDelete === source.id && <div className="message" role="group" aria-label={`删除 ${source.name}`}>
          <p>删除“{source.name}”？停止后续采集，保留已收藏资料和历史版本。</p>
          <div className="source-actions"><button disabled={!!busy} onClick={() => remove(source)}>确认删除</button>
          <button className="subtle" disabled={!!busy} onClick={() => setConfirmDelete('')}>取消</button></div>
        </div>}
        {opened === source.id && source.enabled && <PlatformTools key={source.id} sourceId={source.id} kind={source.kind} onChanged={onChanged} />}
      </article>;
    })}
    {!sources.length && <p className="empty">{loading ? '加载中…' : '当前身份还没有来源。'}</p>}
  </div>;
}
