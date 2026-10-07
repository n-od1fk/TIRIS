export type Item = { id: string; title: string; canonical_url: string; excerpt: string;
  content_type: string; sources: string[]; version: number; favorite: boolean;
  published_at: string | null; evidence_scope: string; reason?: string };

export function ItemCard({ item, busy, onRead, onFavorite }: { item: Item; busy?: boolean;
  onRead: (id: string, version?: number) => void; onFavorite: (id: string, enabled: boolean) => void;
}) {
  return <article className="item">
    <div className="item-meta"><span>{item.sources.join(' / ')}</span><span>{item.content_type === 'repository' ? '代码仓库' : item.content_type === 'release' ? '版本发布' : '资讯'}</span><time>{item.published_at ? new Date(item.published_at).toLocaleDateString('zh-CN') : '日期未知'}</time></div>
    <h2><button className="title-link" onClick={() => onRead(item.id, item.version)}>{item.title}</button></h2>
    {item.excerpt && <p className="excerpt">{item.excerpt}</p>}
    <div className="item-footer"><a href={item.canonical_url} target="_blank" rel="noopener noreferrer">阅读原文 ↗</a><button aria-pressed={item.favorite} disabled={busy} onClick={() => onFavorite(item.id, !item.favorite)}>{item.favorite ? '已收藏' : '收藏'}</button></div>
    <details className="item-details"><summary>推荐依据</summary>{item.reason && <p>{item.reason}</p>}<small>{item.evidence_scope || '仅取得来源元数据'} · v{item.version}</small></details>
  </article>;
}
