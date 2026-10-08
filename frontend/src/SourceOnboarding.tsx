import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './client';

export type Connection = { id: string; name: string; kind: string; url: string; input_url?: string;
  access_type?: string; connection_version?: number; sync_supported?: boolean; validated_at?: string | null;
  credential_configured?: boolean; credential_mode?: string;
  enabled: boolean; last_success_at: string | null; last_error: string | null };
type Preview = { title: string; url: string; author: string | null; published_at: string | null; clean_text: string };
type Candidate = { id: string; url: string; title: string; access_type: string; method: string; verified: boolean;
  preview: Preview[]; error: string };
type Platform = { kind: string; title: string; url: string; description: string; credential_help_url: string;
  credential_fields: { name: string; label: string; required: boolean; help: string }[] };
type Detection = { discovery_id: string; input_url: string; message: string; candidates: Candidate[]; warnings: string[]; needs: string[]; platform?: Platform };
const formats: Record<string, string> = { rss: 'RSS', atom: 'Atom', json_feed: 'JSON Feed', platform_api: '平台 API' };

export function SourceOnboarding({ editing, onSaved, onCancel }: {
  editing: Connection | null; onSaved: (message: string) => Promise<void>; onCancel: () => void;
}) {
  const [url, setUrl] = useState(editing?.input_url || editing?.url || '');
  const [name, setName] = useState(editing?.name || '');
  const [detection, setDetection] = useState<Detection | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [platformDraft, setPlatformDraft] = useState<Detection | null>(null);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [clearCredentials, setClearCredentials] = useState(false);
  const sequence = useRef(0);
  useEffect(() => () => { sequence.current++; }, []);
  async function detect(event: FormEvent) {
    event.preventDefault(); const ticket = ++sequence.current;
    setBusy(true); setError(''); setDetection(null); setSelected([]); setPlatformDraft(null); setCredentials({}); setClearCredentials(false);
    try {
      const result = await api<Detection>('/sources/discover', { method: 'POST', body: JSON.stringify({ url: url.trim() }) }, 120000);
      if (ticket !== sequence.current) return;
      setDetection(result);
      if (result.platform) setPlatformDraft(result);
      const valid = result.candidates.filter(row => row.verified);
      setSelected(valid.length === 1 ? [valid[0].id] : []);
    } catch (e) { if (ticket === sequence.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '检测失败'); }
    finally { if (ticket === sequence.current) setBusy(false); }
  }
  async function verifyPlatform() {
    if (!platformDraft) return;
    const ticket = ++sequence.current; setBusy(true); setError(''); setSelected([]);
    try {
      const supplied = Object.fromEntries(Object.entries(credentials).filter(([, value]) => value.trim()));
      const result = await api<Detection>('/sources/verify-platform', { method: 'POST', body: JSON.stringify({
        discovery_id: platformDraft.discovery_id, credentials: supplied,
        credential_action: Object.keys(supplied).length ? 'replace' : clearCredentials ? 'clear' : 'keep',
        ...(editing ? { edit_source_id: editing.id, expected_version: editing.connection_version || 1 } : {}),
      }) }, 120000);
      if (ticket !== sequence.current) return;
      setDetection(result); setSelected(result.candidates.filter(row => row.verified).map(row => row.id)); setCredentials({});
    } catch (e) { if (ticket === sequence.current && !(e instanceof DOMException && e.name === 'AbortError')) { setDetection(platformDraft); setError(e instanceof Error ? e.message : '平台验证失败'); } }
    finally { if (ticket === sequence.current) setBusy(false); }
  }
  async function save(renameOnly = false) {
    const ticket = ++sequence.current; setBusy(true); setError('');
    try {
      if (renameOnly && editing) {
        await api(`/sources/${editing.id}`, { method: 'PATCH', body: JSON.stringify({ name: name.trim(), expected_version: editing.connection_version || 1 }) });
      } else {
        if (!detection) return;
        await api('/sources/connect', { method: 'POST', body: JSON.stringify({
          discovery_id: detection.discovery_id,
          selections: selected.map(id => ({ candidate_id: id, name: (name.trim() && selected.length > 1 ? `${name.trim()} · ${detection.candidates.find(c => c.id === id)!.title}` : name.trim()).slice(0, 200) })),
          enabled: editing?.enabled ?? true,
          ...(editing ? { edit_source_id: editing.id, expected_version: editing.connection_version || 1 } : {}),
        }) }, 180000);
      }
      if (ticket !== sequence.current) return;
      setDetection(null); setSelected([]); setPlatformDraft(null); setCredentials({}); setUrl(''); setName('');
      await onSaved(editing ? '来源配置已保存，历史和收藏保留。' : '已验证并保存来源，内容已获取到候选区，未自动收藏。');
    } catch (e) { if (ticket === sequence.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '保存失败'); }
    finally { if (ticket === sequence.current) setBusy(false); }
  }
  return <form className="settings-panel" onSubmit={detect}>
    <div className="section-heading"><h2>{editing ? '编辑来源' : '添加来源'}</h2>{editing && <button type="button" className="subtle" disabled={busy} onClick={onCancel}>取消编辑</button>}</div>
    {error && <p className="failure" role="alert">{error}</p>}
    <fieldset disabled={busy}>
      <label>来源地址<input aria-label="来源地址" required maxLength={2000} value={url} onChange={e => { sequence.current++; setUrl(e.target.value); setDetection(null); setSelected([]); setPlatformDraft(null); setCredentials({}); setClearCredentials(false); setError(''); }} placeholder="网站、平台、栏目或订阅源地址" /></label>
      <label>来源名称<input aria-label="来源名称" maxLength={200} value={name} onChange={e => setName(e.target.value)} placeholder="可选，留空使用识别到的订阅名称" /></label>
      <div className="source-actions"><button type="submit">{busy ? '处理中…' : '检测接入方式'}</button>
        {editing && url.trim() === (editing.input_url || editing.url) && <button type="button" className="subtle" disabled={!name.trim()} onClick={() => save(true)}>仅保存名称</button>}
      </div>
      {detection && <section className="source-detection" aria-label="接入识别结果">
        <p>{detection.message}</p>
        {detection.warnings.map((text, i) => <p className="scope" key={i}>{text}</p>)}
        {detection.needs.map((text, i) => <p className="failure" key={i}>{text}</p>)}
        {platformDraft?.platform && <div className="feed-choice">
          <h3>{platformDraft.platform.title}</h3><p className="scope">{platformDraft.platform.description}</p>
          {editing?.credential_configured && <p className="scope">此来源已有凭证，留空保留；输入新值会替换。页面不会回显已有 Token。</p>}
          {platformDraft.platform.credential_fields.map(field => <label key={field.name}>{field.label}
            <input aria-label={field.label} type="password" autoComplete="new-password" maxLength={4096} disabled={clearCredentials} required={field.required && !editing?.credential_configured} value={credentials[field.name] || ''}
              onChange={e => { setCredentials(values => ({ ...values, [field.name]: e.target.value })); setDetection(platformDraft); setSelected([]); }} placeholder={editing?.credential_configured ? '留空保留已有凭证' : '可选，留空使用匿名公开读取'} />
            <small>{field.help}</small>
          </label>)}
          {editing?.credential_configured && <label className="feed-selection"><input type="checkbox" checked={clearCredentials} onChange={e => { setClearCredentials(e.target.checked); setCredentials({}); setDetection(platformDraft); setSelected([]); }} />移除凭证，改用匿名读取</label>}
          <p className="scope">凭证仅发给本机后端，加密保存；不要把无登录服务开放到公网。</p>
          <div className="source-actions"><a href={platformDraft.platform.credential_help_url} target="_blank" rel="noopener noreferrer">申请平台凭证</a>
            <button type="button" disabled={detection.candidates.some(row => row.verified)} onClick={verifyPlatform}>验证平台连接</button></div>
        </div>}
        {detection.candidates.map(candidate => <div className="feed-choice" key={candidate.id}>
          <label className="feed-selection"><input type="checkbox" disabled={!candidate.verified} checked={selected.includes(candidate.id)} onChange={e => setSelected(values => e.target.checked ? editing ? [candidate.id] : [...values, candidate.id] : values.filter(id => id !== candidate.id))} />
            <span>{candidate.title} · {candidate.verified ? `${formats[candidate.access_type] || candidate.access_type} 已验证` : '未通过验证'}</span></label>
          <a className="source-url" href={candidate.url} target="_blank" rel="noopener noreferrer">{candidate.url}</a>
          {candidate.error && <p className="failure">{candidate.error}</p>}
          {candidate.verified && !candidate.preview.length && <p className="scope">接入已验证，当前没有条目。</p>}
          {candidate.preview.map(row => <details key={row.url}><summary>{row.title}</summary>
            <p className="scope">{row.author || '作者未知'} · {row.published_at ? new Date(row.published_at).toLocaleString('zh-CN') : '发布时间未知'}</p>
            <a href={row.url} target="_blank" rel="noopener noreferrer">查看原文</a>
            <p className="source-preview">{row.clean_text || '订阅未提供正文或摘要'}</p>
            <small>来源实际提供的内容预览，可能为摘要或元数据，不代表原文全文。</small>
          </details>)}
        </div>)}
        <button type="button" disabled={!selected.length} onClick={() => save()}>{editing ? '验证并保存修改' : `验证并保存所选来源${selected.length ? `（${selected.length}）` : ''}`}</button>
      </section>}
    </fieldset>
  </form>;
}
