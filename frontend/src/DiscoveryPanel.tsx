import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './client';
import { ItemCard } from './ItemCard';
import type { Item } from './ItemCard';

type Run = { id: string; status: string; error: string | null; report: { recommendation_count?: number; warnings?: string[]; source_results?: { status: string; error: string | null }[] } };
type Batch = { state: 'running' | 'waiting' | 'needs_profile'; run: Run | null; retry_after: number };
type Page = { items: Item[]; next_cursor: string | null };

export function DiscoveryPanel({ active, revision, onRead, onFavorite, onSettings }: {
  active: boolean; revision: number; onRead: (id: string, version?: number) => void;
  onFavorite: (id: string, enabled: boolean) => Promise<void>; onSettings: () => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState('');
  const [retryAt, setRetryAt] = useState(0);
  const [problems, setProblems] = useState<string[]>([]);
  const inflight = useRef(false);
  const end = useRef<HTMLDivElement>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const fail = (e: unknown) => { if (alive.current && !(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '暂时无法加载'); };
  const merge = (page: Page, reset = false) => {
    if (!alive.current) return;
    setItems(previous => { const rows = new Map((reset ? [] : previous).map(item => [item.id, item])); page.items.forEach(item => rows.set(item.id, item)); return [...rows.values()]; });
    setCursor(page.next_cursor); setLoaded(true);
  };
  async function refill() {
    if (inflight.current || Date.now() < retryAt || batch?.state === 'running') return;
    inflight.current = true; setLoading(true); setError('');
    try {
      const data = await api<Batch>('/feed/refill', { method: 'POST' });
      if (!alive.current) return;
      setBatch(data); setRetryAt(Date.now() + data.retry_after * 1000);
      if (data.run?.error) setError(data.run.error);
      if (data.state === 'waiting') setProblems(data.run?.report.warnings || []);
    } catch (e) { fail(e); setRetryAt(Date.now() + 60000); }
    finally { inflight.current = false; if (alive.current) setLoading(false); }
  }
  async function initial(reset = false) {
    if (inflight.current) return;
    inflight.current = true; setLoading(true);
    try { merge(await api<Page>('/feed'), reset); } catch (e) { fail(e); }
    finally { inflight.current = false; if (alive.current) setLoading(false); }
  }
  useEffect(() => { initial(); }, [revision]);
  useEffect(() => {
    if (!active || !loaded || loading || cursor || batch?.state === 'needs_profile' || batch?.state === 'running') return;
    const timer = setTimeout(() => refill(), Math.max(0, retryAt - Date.now()));
    return () => clearTimeout(timer);
  }, [active, loaded, loading, cursor, retryAt, batch?.state, revision]);
  useEffect(() => { if (revision) { setBatch(null); setRetryAt(0); } }, [revision]);
  useEffect(() => {
    if (batch?.state !== 'running' || !batch.run) return;
    const timer = setInterval(async () => {
      try {
        const runs = await api<Run[]>('/discovery/runs');
        const run = runs.find(row => row.id === batch.run?.id);
        if (run && !['queued', 'running'].includes(run.status)) {
          clearInterval(timer);
          if (!alive.current) return;
          setProblems([...(run.report.warnings || []), ...(run.report.source_results || []).filter(row => row.status !== 'succeeded' && row.error).map(row => row.error!)]);
          if (run.error) setError(run.error);
          setBatch({ state: 'waiting', run, retry_after: 0 });
          setRetryAt(Date.now() + (run.report.recommendation_count ? 2000 : 900000));
          await initial();
        }
      } catch (e) { fail(e); }
    }, 3000);
    return () => clearInterval(timer);
  }, [batch?.state, batch?.run?.id]);
  const more = useCallback(async () => {
    if (!active || !cursor || inflight.current) return;
    inflight.current = true; setLoading(true); setError('');
    try { merge(await api<Page>(`/feed?cursor=${encodeURIComponent(cursor)}`)); }
    catch (e) { fail(e); }
    finally { inflight.current = false; if (alive.current) setLoading(false); }
  }, [active, cursor]);
  useEffect(() => {
    if (!end.current || !active || !cursor) return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) more(); }, { rootMargin: '500px' });
    observer.observe(end.current); return () => observer.disconnect();
  }, [active, cursor, more]);
  return <div className="feed">
    <div className="section-heading"><h1>今日推荐</h1><button className="subtle" disabled={loading} onClick={() => { setRetryAt(0); setBatch(null); initial(true); }}>刷新</button></div>
    {error && <div className="message error" role="alert">{error}<button onClick={() => { setError(''); initial(); }}>重试</button></div>}
    {items.map(item => <ItemCard key={item.id} item={item} busy={saving === item.id} onRead={onRead} onFavorite={async (id, enabled) => {
      setSaving(id); try { await onFavorite(id, enabled); setItems(rows => rows.map(row => row.id === id ? { ...row, favorite: enabled } : row)); } catch (e) { fail(e); } finally { setSaving(''); }
    }} />)}
    <div ref={end} className="feed-end" role="status">
      {loading || batch?.state === 'running' ? '正在补充资讯…' : batch?.state === 'needs_profile' ? <><p>填写关注方向，开始推荐。</p><button onClick={onSettings}>填写画像</button></> : !items.length && !loaded ? '正在加载…' : !cursor ? '暂时没有新资讯，稍后自动补充。' : <button className="subtle" onClick={more}>加载更多</button>}
    </div>
    {problems.length > 0 && <details className="feed-problems"><summary>部分来源暂不可用</summary>{problems.map((problem, i) => <p key={i}>{problem}</p>)}</details>}
  </div>;
}
