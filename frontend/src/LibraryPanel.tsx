import { useEffect, useState } from 'react';
import { api } from './client';
import { UpdatesPanel } from './UpdatesPanel';
import { ItemCard } from './ItemCard';
import type { Item } from './ItemCard';

export function LibraryPanel({ active, revision, onRead, onFavorite }: { active: boolean; revision: number;
  onRead: (id: string, version?: number) => void; onFavorite: (id: string, enabled: boolean) => Promise<void>;
}) {
  const [tab, setTab] = useState<'saved' | 'updates'>('saved');
  const [data, setData] = useState<{ items: Item[]; total: number }>({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState('');
  const [error, setError] = useState('');
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : '读取失败');
  useEffect(() => {
    if (!active || tab !== 'saved') return;
    const controller = new AbortController(); setLoading(true);
    api<typeof data>(`/items?scope=library&offset=${offset}&q=${encodeURIComponent(query)}`, { signal: controller.signal }).then(setData).catch(e => { if (!controller.signal.aborted) fail(e); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, tab, revision, offset, query]);
  return <div><div className="section-heading"><h1>资料库</h1><div className="library-tabs"><button aria-pressed={tab === 'saved'} onClick={() => setTab('saved')}>收藏</button><button aria-pressed={tab === 'updates'} onClick={() => setTab('updates')}>更新提醒</button></div></div>
    {tab === 'updates' ? <UpdatesPanel onRead={onRead} /> : <>
      <form className="filters" onSubmit={e => { e.preventDefault(); setQuery(search); setOffset(0); }}><input aria-label="查找收藏" placeholder="查找收藏" value={search} maxLength={200} onChange={e => setSearch(e.target.value)} /><button>查找</button></form>
      {error && <div role="alert" className="message error">{error}</div>}
      {data.items.map(item => <ItemCard key={item.id} item={item} busy={saving === item.id} onRead={onRead} onFavorite={async (id, enabled) => { setSaving(id); try { await onFavorite(id, enabled); if (!enabled) setData(previous => ({ total: previous.total - 1, items: previous.items.filter(row => row.id !== id) })); } catch (e) { fail(e); } finally { setSaving(''); } }} />)}
      {!data.items.length && <p className="empty">{loading ? '加载中…' : '还没有收藏。'}</p>}
      {data.total > 20 && <nav className="pagination" aria-label="收藏分页"><button disabled={!offset || loading} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><button disabled={offset + 20 >= data.total || loading} onClick={() => setOffset(n => n + 20)}>下一页</button></nav>}
    </>}
  </div>;
}
