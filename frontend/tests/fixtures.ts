import type { Page } from '@playwright/test';

export const primary = 'ea83e192-1223-4755-a219-33ca3bce34bc';
export const other = '00000000-0000-4000-a000-000000000002';
export const item = (index = 1) => ({ id: `item-${index}`, title: `技术资讯 ${index}`, canonical_url: `https://example.com/${index}`,
  content_type: 'article', sources: ['测试来源'], excerpt: '来源内容节选', evidence_scope: 'RSS excerpt only',
  published_at: '2026-10-07T00:00:00Z', version: 1, favorite: false, reason: '匹配长期关注方向' });
export const emptyProfile = () => ({ version: 0, data: { background: '', foundations: [], long_term_directions: [], learning_goals: [], blocked_topics: [], language_preferences: ['zh'], daily_reading_minutes: 30 } });
export const sourceTypes = [
  { kind: 'rss', label: 'RSS / Atom', hint: '填写 RSS/Atom 订阅地址，不是网站首页。', placeholder: 'https://example.com/feed.xml' },
  { kind: 'github', label: 'GitHub 仓库', hint: '填写具体仓库地址', placeholder: 'https://github.com/owner/repo' },
  { kind: 'blackhat', label: 'Black Hat 索引', hint: '只读取公开索引，访问受限会显示错误。', placeholder: 'https://www.blackhat.com/html/archives.html' },
];

export async function mockApp(page: Page, options: { pages?: number; conflict?: boolean; delayed?: boolean; needsProfile?: boolean } = {}) {
  const state = { saved: false, read: false, profile: emptyProfile(), identities: [{ id: primary, name: '我的身份' }, { id: other, name: 'AI 学习' }], requests: [] as { method: string; path: string; user: string; body: any }[] };
  await page.route('**/api/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    const user = request.headers()['x-tiris-user'] || primary; const method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ method, path, user, body });
    if (path === '/api/identities' && method === 'POST') {
      const row = { id: '00000000-0000-4000-a000-000000000003', name: body.name }; state.identities.push(row); await route.fulfill({ status: 201, json: row }); return;
    }
    if (path.startsWith('/api/identities/') && method === 'PATCH') {
      const row = state.identities.find(identity => identity.id === path.split('/').at(-1))!; row.name = body.name; await route.fulfill({ json: row }); return;
    }
    if (path === '/api/profile' && method === 'PATCH') {
      if (options.conflict) { await route.fulfill({ status: 409, json: { detail: { message: '画像版本冲突' } } }); return; }
      const { expected_version, ...data } = body; state.profile = { version: expected_version + 1, data }; await route.fulfill({ json: state.profile }); return;
    }
    if (path.endsWith('/favorite')) { state.saved = body.enabled; await route.fulfill({ json: { favorite: state.saved } }); return; }
    if (path.endsWith('/read')) { state.read = true; await route.fulfill({ json: { read: true } }); return; }
    if (path === '/api/feed') {
      if (user !== primary) { await route.fulfill({ json: { items: [{ ...item(99), title: '另一个身份的 AI 资讯' }], next_cursor: null } }); return; }
      const cursor = Number(url.searchParams.get('cursor') || '0');
      if (options.delayed && cursor) await new Promise(resolve => setTimeout(resolve, 800));
      const pages = options.pages || 1; const count = options.pages ? 20 : 1;
      const items = Array.from({ length: count }, (_, i) => ({ ...item(cursor * 20 + i + 1), favorite: cursor === 0 && i === 0 && state.saved }));
      try { await route.fulfill({ json: { items, next_cursor: cursor + 1 < pages ? String(cursor + 1) : null } }); } catch { /* Identity switch may have aborted this response. */ }
      return;
    }
    if (path === '/api/feed/refill') {
      await route.fulfill({ status: 202, json: { state: options.needsProfile ? 'needs_profile' : 'waiting', retry_after: 900, run: null } }); return;
    }
    if (path.startsWith('/api/items/')) {
      await route.fulfill({ json: { ...item(), favorite: state.saved, version: 2,
        versions: [{ number: 1, title: item().title, text: '原推荐版本正文', evidence_scope: 'RSS excerpt only' }, { number: 2, title: '后来的版本', text: '后来更新的正文', evidence_scope: 'RSS excerpt only' }],
        sources: [{ name: '测试来源', original_url: item().canonical_url, external_id: 'one' }] } }); return;
    }
    const data = path === '/api/identities' ? { default_id: primary, items: state.identities } :
      path === '/api/source-types' ? sourceTypes :
      path === '/api/profile' ? (user === primary ? state.profile : emptyProfile()) :
      path === '/api/items' ? { total: Number(state.saved && user === primary), items: state.saved && user === primary ? [{ ...item(), favorite: true }] : [] } :
      path === '/api/updates' ? { total: 1, unread: Number(!state.read), items: [{ id: 'notice-one', item_id: 'item-1', title: '已收藏文章更新', url: item().canonical_url, message: '已获取内容发生变化：v1 → v2', read: state.read, version: 2, created_at: '2026-10-07T00:00:00Z', evidence_scope: 'RSS excerpt only' }] } : [];
    await route.fulfill({ json: data });
  });
  return state;
}
