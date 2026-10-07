import { useEffect, useState } from 'react';
import { api } from './client';

type Notice = { id: string; item_id: string; title: string; url: string; message: string;
  evidence_scope: string; read: boolean; created_at: string; version: number };
export function UpdatesPanel({ onRead }: { onRead: (id: string, version?: number) => void }) {
  const [data, setData] = useState<{ total: number; unread: number; items: Notice[] }>({ total: 0, unread: 0, items: [] });
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false); const [offset, setOffset] = useState(0);
  const [checkId, setCheckId] = useState('');
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : '更新服务不可用');
  async function load() { setData(await api<typeof data>(`/updates?offset=${offset}`)); }
  useEffect(() => { load().catch(fail); }, [offset]);
  useEffect(() => {
    if (!checkId) return;
    const timer = setInterval(async () => {
      try {
        const rows = await api<{ id: string; status: string; error: string | null }[]>('/discovery/runs');
        const run = rows.find(r => r.id === checkId);
        if (run && !['queued', 'running'].includes(run.status)) {
          setCheckId(''); setNotice(run.status === 'succeeded' ? '检查完成' : '检查结束，部分来源可能未完成');
          if (run.error) setError(run.error);
          await load();
        }
      } catch (e) { fail(e); }
    }, 3000);
    return () => clearInterval(timer);
  }, [checkId, offset]);
  return <section><div className="section-heading"><h2>收藏更新提醒</h2><span>未读 {data.unread}</span></div>
    {error && <div role="alert" className="message error">{error}</div>}{notice && <div role="status" className="message">{notice}</div>}
    <div className="source-actions"><button disabled={busy || !!checkId} onClick={async () => {
      setBusy(true); setError('');
      try { const run = await api<{ id: string }>('/discovery/check-updates', { method: 'POST' }); setCheckId(run.id); setNotice('正在检查…'); }
      catch (e) { fail(e); } finally { setBusy(false); }
    }}>检查收藏更新</button><button className="subtle" onClick={() => load().catch(fail)}>刷新提醒</button></div>
    {data.items.map(item => <article className="item" key={item.id}><h3><button className="title-link" onClick={() => onRead(item.item_id, item.version)}>{item.title}</button></h3>
      <p>{item.message}</p><p className="scope">{item.evidence_scope} · {new Date(item.created_at).toLocaleString('zh-CN')}</p>
      <div className="source-actions">{!item.read && <button onClick={async () => { try { await api(`/updates/${item.id}/read`, { method: 'PATCH' }); await load(); } catch (e) { fail(e); } }}>标记已读</button>}<a href={item.url} target="_blank" rel="noopener noreferrer">查看原文 ↗</a></div></article>)}
    {!data.items.length && <p className="empty">暂无更新。</p>}
    {data.total > 20 && <nav className="pagination"><button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><span>共 {data.total} 条</span><button disabled={offset + 20 >= data.total} onClick={() => setOffset(n => n + 20)}>下一页</button></nav>}
  </section>;
}
