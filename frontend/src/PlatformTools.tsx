import { useState } from 'react';
import { api, downloadMaterial } from './client';

type Candidate = { url: string; title: string; acquisition_level: string };
type Page = { items: Candidate[]; next_cursor: string | null; scope: string };
type Document = { item_id: string; title: string; original_url: string; body: string; status: string; reason: string;
  materials: { id: string; filename: string; download_url: string }[];
  attachments: { url: string; label: string; downloaded: boolean }[] };
const labels: Record<string, string> = { list: '浏览列表', search: '平台查询', stars: 'Star 列表', releases: 'Releases' };

export function PlatformTools({ sourceId, kind, onChanged }: { sourceId: string; kind: string; onChanged: () => void }) {
  const [operations, setOperations] = useState<string[]>([]);
  const [operation, setOperation] = useState('list');
  const [query, setQuery] = useState('');
  const [subject, setSubject] = useState('');
  const [target, setTarget] = useState('');
  const [page, setPage] = useState<Page | null>(null);
  const [document, setDocument] = useState<Document | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function run(work: () => Promise<void>) {
    setBusy(true); setError('');
    try { await work(); } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : '平台访问失败');
    } finally { setBusy(false); }
  }
  async function load(cursor = '') {
    await run(async () => {
      let selectedOperation = operation;
      if (!operations.length) {
        const capabilities = await api<{ operations: string[]; credential_env: string | null; credential_configured: boolean | null }>(`/platforms/${sourceId}/capabilities`);
        setOperations(capabilities.operations);
        if (!capabilities.operations.includes(selectedOperation)) {
          selectedOperation = capabilities.operations[0]; setOperation(selectedOperation);
        }
        setNotice(capabilities.credential_env && !capabilities.credential_configured ? '公开读取可先测试；需要 Token 时在来源页面点击“编辑来源”，检测后填写并验证。' : '凭证只在后端使用，页面不回显。');
      }
      setPage(await api<Page>(`/platforms/${sourceId}/query`, { method: 'POST', body: JSON.stringify({ operation: selectedOperation, query, subject, cursor, limit: 5 }) }, 60000));
    });
  }
  async function detail(url: string) {
    await run(async () => {
      const result = await api<Document>(`/platforms/${sourceId}/detail`, { method: 'POST', body: JSON.stringify({ url, download_attachments: kind === 'blackhat' }) }, 180000);
      setDocument(result); onChanged();
    });
  }
  return <div className="platform-tools">
    {error && <p className="failure" role="alert">{error}</p>}
    {notice && <p className="scope">{notice}</p>}
    <div className="source-fields">
      <label>平台操作<select value={operation} onChange={e => { setOperation(e.target.value); setPage(null); }}>
        {(operations.length ? operations : kind === 'github' ? ['list', 'search', 'stars', 'releases'] : kind === 'arxiv' ? ['list', 'search'] : ['list']).map(op => <option key={op} value={op}>{labels[op]}</option>)}
      </select></label>
      <label>查询条件<input value={query} maxLength={500} onChange={e => { setQuery(e.target.value); setPage(null); }} placeholder="调用方指定条件，不自动推断兴趣" /></label>
    </div>
    <label>操作范围<input value={subject} maxLength={2000} onChange={e => { setSubject(e.target.value); setPage(null); }} placeholder={kind === 'github' ? '用户名；Releases 操作填写仓库链接' : '网页/Black Hat 可填写活动或列表页地址'} /></label>
    <div className="source-actions"><button disabled={busy} onClick={() => load()}>{busy ? '请求中…' : '获取列表'}</button>
      {page?.next_cursor && <button disabled={busy} onClick={() => load(page.next_cursor!)}>下一页</button>}</div>
    {page && <p className="scope">{page.scope} · 列表未自动收藏</p>}
    {page?.items.map(row => <div className="platform-candidate" key={row.url}>
      <a href={row.url} target="_blank" rel="noopener noreferrer">{row.title}</a>
      <button className="subtle" disabled={busy} onClick={() => { setTarget(row.url); detail(row.url); }}>读取正文与原文件</button>
    </div>)}
    <label>原文地址<input value={target} maxLength={2000} onChange={e => setTarget(e.target.value)} placeholder="也可以直接指定平台内的原文链接" /></label>
    <button className="subtle" disabled={busy || !target} onClick={() => detail(target)}>读取指定内容</button>
    {document && <section className="platform-document">
      <h3>{document.title}</h3>
      <p className="scope">{document.status === 'full' ? '正文提取完成（自动检查，非人工保证）' : '内容不完整'}{document.reason && ` · ${document.reason}`} · 尚未生成 AI 摘要</p>
      <a href={document.original_url} target="_blank" rel="noopener noreferrer">查看原文</a>
      <button className="subtle" disabled={busy} onClick={() => run(async () => {
        await api(`/items/${document.item_id}/favorite`, { method: 'PUT', body: JSON.stringify({ enabled: true }) });
        setNotice('已收藏到当前身份的资料库。'); onChanged();
      })}>收藏到资料库</button>
      <div className="source-actions">{document.materials.map(m => <button className="subtle" key={m.id} disabled={busy} onClick={() => run(() => downloadMaterial(m.download_url, m.filename))}>下载 {m.filename}</button>)}</div>
      {document.attachments.map(a => <p key={a.url}><a href={a.url} target="_blank" rel="noopener noreferrer">{a.label || '附件'}</a> · {a.downloaded ? '已保存原文件' : '仅原始链接'}</p>)}
      <pre className="platform-body">{document.body || '未取得正文'}</pre>
    </section>}
  </div>;
}
