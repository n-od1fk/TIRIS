import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, downloadMaterial, selectIdentity } from './client';
import { DiscoveryPanel } from './DiscoveryPanel';
import { LibraryPanel } from './LibraryPanel';
import { SourcesPanel } from './SourcesPanel';
import { SettingsPanel } from './SettingsPanel';
import type { Identity } from './SettingsPanel';
import './style.css';

type Detail = { id: string; title: string; favorite: boolean; version: number;
  versions: { number: number; title: string; text: string; evidence_scope: string;
    materials?: { filename: string; download_url: string }[] }[];
  sources: { name: string; original_url: string; external_id: string }[] };

function Workspace({ identities, current, onSwitch, onIdentities }: {
  identities: Identity[]; current: string; onSwitch: (id: string) => void; onIdentities: (rows: Identity[]) => void;
}) {
  const [view, setView] = useState<'feed' | 'library' | 'sources' | 'settings'>('feed');
  const [settingsOpened, setSettingsOpened] = useState(false);
  const [libraryOpened, setLibraryOpened] = useState(false);
  const [sourcesOpened, setSourcesOpened] = useState(false);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Detail | null>(null);
  const [versionNumber, setVersionNumber] = useState(0);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => { if (!(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '暂时无法读取'); };
  async function read(id: string, version?: number) {
    try { const row = await api<Detail>(`/items/${id}`); setDetail(row); setVersionNumber(version ?? row.version); }
    catch (e) { fail(e); }
  }
  async function favorite(id: string, enabled: boolean) {
    await api(`/items/${id}/favorite`, { method: 'PUT', body: JSON.stringify({ enabled }) });
    setRevision(n => n + 1);
    setDetail(row => row?.id === id ? { ...row, favorite: enabled } : row);
  }
  const version = detail?.versions.find(v => v.number === versionNumber);
  return <div className="app-layout">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">T</span><strong>TIRIS</strong></div>
      <nav aria-label="主导航">
        <button aria-current={view === 'feed' ? 'page' : undefined} onClick={() => setView('feed')}>今日推荐</button>
        <button aria-current={view === 'library' ? 'page' : undefined} onClick={() => { setLibraryOpened(true); setView('library'); }}>资料库</button>
        <button aria-current={view === 'sources' ? 'page' : undefined} onClick={() => { setSourcesOpened(true); setView('sources'); }}>来源</button>
        <button aria-current={view === 'settings' ? 'page' : undefined} onClick={() => { setSettingsOpened(true); setView('settings'); }}>设置</button>
      </nav>
      <label className="identity-label">身份<select aria-label="切换身份" value={current} onChange={e => onSwitch(e.target.value)}>{identities.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
    </aside>
    <main className="main-content">
      {error && <div className="message error" role="alert">{error}<button onClick={() => setError('')}>关闭</button></div>}
      <section hidden={view !== 'feed'}><DiscoveryPanel active={view === 'feed'} revision={revision} onRead={read} onFavorite={favorite} onSettings={() => { setSettingsOpened(true); setView('settings'); }} /></section>
      {libraryOpened && <section hidden={view !== 'library'}><LibraryPanel active={view === 'library'} revision={revision} onRead={read} onFavorite={favorite} /></section>}
      {sourcesOpened && <section hidden={view !== 'sources'}><SourcesPanel active={view === 'sources'} onChanged={() => setRevision(n => n + 1)} /></section>}
      {settingsOpened && <section hidden={view !== 'settings'}><SettingsPanel identities={identities} current={current} onSwitch={onSwitch} onIdentities={onIdentities} onSaved={() => setRevision(n => n + 1)} /></section>}
    </main>
    {detail && <div className="overlay"><section className="reader" role="dialog" aria-modal="true" aria-labelledby="reader-title">
      <div className="section-heading"><h2 id="reader-title">{version?.title || detail.title}</h2><button onClick={() => setDetail(null)}>关闭阅读</button></div>
      <div className="reader-controls"><label>版本<select value={versionNumber} onChange={e => setVersionNumber(Number(e.target.value))}>{detail.versions.map(v => <option key={v.number} value={v.number}>v{v.number}</option>)}</select></label>
        <button disabled={busy} onClick={async () => { setBusy(true); try { await favorite(detail.id, !detail.favorite); } catch (e) { fail(e); } finally { setBusy(false); } }}>{detail.favorite ? '取消收藏' : '收藏'}</button></div>
      <small>{version?.evidence_scope}</small>
      <div className="source-actions">{version?.materials?.map(file => <button className="subtle" key={file.download_url} disabled={busy} onClick={async () => {
        setBusy(true); try { await downloadMaterial(file.download_url, file.filename); } catch (e) { fail(e); } finally { setBusy(false); }
      }}>下载 {file.filename}</button>)}</div>
      <p className="reader-text">{version?.text || '未获取正文，请阅读原文。'}</p>
      {detail.sources.map(source => <p key={`${source.name}-${source.external_id}`}><a href={source.original_url} target="_blank" rel="noopener noreferrer">{source.name} · 原文 ↗</a></p>)}
    </section></div>}
  </div>;
}

function App() {
  const [identities, setIdentities] = useState<Identity[]>([]);
  const [current, setCurrent] = useState('');
  const [error, setError] = useState('');
  async function load() {
    try {
      const data = await api<{ default_id: string; items: Identity[] }>('/identities');
      const stored = localStorage.getItem('tiris-identity');
      const id = data.items.some(row => row.id === stored) ? stored! : data.default_id;
      selectIdentity(id); setIdentities(data.items); setCurrent(id); setError('');
    } catch (e) { setError(e instanceof Error ? e.message : '加载失败'); }
  }
  useEffect(() => { load(); document.documentElement.dataset.theme = localStorage.getItem('tiris-theme') || 'light'; }, []);
  if (!current) return <div className="startup">{error ? <><p role="alert">{error}</p><button onClick={load}>重试</button></> : '加载中…'}</div>;
  return <Workspace key={current} identities={identities} current={current} onIdentities={setIdentities} onSwitch={id => { selectIdentity(id); setCurrent(id); }} />;
}

createRoot(document.getElementById('root')!).render(<App />);
