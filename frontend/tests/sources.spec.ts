import { expect, test } from '@playwright/test';
import { item, mockApp, other, primary, sourceTypes } from './fixtures';

async function sourcesApp(page: import('@playwright/test').Page) {
  const state = await mockApp(page);
  const sources: Record<string, any[]> = {
    [primary]: [{ id: 'source-one', name: '安全博客', kind: 'rss', url: 'https://example.com/rss', enabled: true, last_success_at: null, last_error: null }],
    [other]: [{ id: 'source-two', name: 'AI 仓库', kind: 'github', url: 'https://github.com/example/ai', enabled: true, last_success_at: null, last_error: null }],
  };
  const jobs: Record<string, any[]> = { [primary]: [], [other]: [] };
  const writes: any[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const user = request.headers()['x-tiris-user'] || primary;
    const method = request.method(); const body = request.postData() ? request.postDataJSON() : null;
    if (path === '/api/sources/sync') {
      writes.push({ user, path, body });
      const job = { id: 'job-one', source_id: body.source_id, status: 'queued', error: null };
      jobs[user] = [job]; await route.fulfill({ status: 202, json: job }); return;
    }
    if (path.startsWith('/api/sources/') && method === 'PATCH') {
      writes.push({ user, path, body }); const row = sources[user].find(source => source.id === path.split('/').at(-1));
      Object.assign(row, body); await route.fulfill({ json: row }); return;
    }
    if (path === '/api/sources') {
      if (method === 'POST') {
        writes.push({ user, path, body });
        if (sources[user].some(source => source.url === body.url)) { await route.fulfill({ status: 409, json: { detail: '这个来源已经添加过了' } }); return; }
        const row = { ...body, id: `source-${sources[user].length + 3}`, enabled: true, last_success_at: null, last_error: null };
        sources[user].push(row); await route.fulfill({ status: 201, json: row }); return;
      }
      await route.fulfill({ json: sources[user] || [] }); return;
    }
    if (path === '/api/jobs') { await route.fulfill({ json: jobs[user] || [] }); return; }
    if (path === '/api/items' && user === other) { await route.fulfill({ json: { total: 1, items: [{ ...item(99), title: 'AI 已收藏资料', favorite: true }] } }); return; }
    if (path === '/api/updates' && user === other) { await route.fulfill({ json: { total: 0, unread: 0, items: [] } }); return; }
    await route.fallback();
  });
  return { state, sources, jobs, writes };
}

test('sidebar identity switch changes populated feed, library and sources and switches back', async ({ page }) => {
  const { state } = await sourcesApp(page); state.saved = true;
  await page.goto('/');
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.locator('.main-content > section:not([hidden]) .item')).toContainText('技术资讯 1');
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.locator('.source-card')).toContainText('安全博客');
  await page.getByLabel('切换身份', { exact: true }).selectOption(other);
  await expect(page.getByRole('button', { name: '另一个身份的 AI 资讯', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.locator('.main-content > section:not([hidden]) .item')).toContainText('AI 已收藏资料');
  await expect(page.locator('.main-content > section:not([hidden])')).not.toContainText('技术资讯 1');
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.locator('.source-card')).toContainText('AI 仓库');
  await expect(page.locator('.sources-view')).not.toContainText('安全博客');
  await page.getByLabel('切换身份', { exact: true }).selectOption(primary);
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.locator('.main-content > section:not([hidden]) .item')).toContainText('技术资讯 1');
});

test('source form supports explicit kinds, keeps duplicate drafts and toggles only current source', async ({ page }) => {
  const { writes } = await sourcesApp(page);
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
  expect(writes).toHaveLength(0);
  await page.getByLabel('来源名称', { exact: true }).fill('Black Hat');
  await page.getByLabel('来源类型', { exact: true }).selectOption('blackhat');
  await page.getByLabel('来源地址', { exact: true }).fill('https://www.blackhat.com/html/archives.html');
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已添加到当前身份');
  await expect(page.locator('.source-card')).toHaveCount(2);
  expect(writes).toEqual([{ user: primary, path: '/api/sources', body: { name: 'Black Hat', kind: 'blackhat', url: 'https://www.blackhat.com/html/archives.html' } }]);
  const card = page.locator('.source-card').filter({ has: page.getByRole('heading', { name: 'Black Hat', exact: true }) });
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByRole('button', { name: '同步', exact: true })).toBeDisabled();
  await card.getByRole('button', { name: '启用', exact: true }).click();
  await expect(card.getByRole('button', { name: '同步', exact: true })).toBeEnabled();
  await page.getByLabel('来源名称', { exact: true }).fill('重复但要保留');
  await page.getByLabel('来源地址', { exact: true }).fill('https://www.blackhat.com/html/archives.html');
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('已经添加过');
  await expect(page.getByLabel('来源名称', { exact: true })).toHaveValue('重复但要保留');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/sources-mobile.png' });
});

test('manual source sync reports queued state then an honest fetch failure', async ({ page }) => {
  const { jobs, writes } = await sourcesApp(page);
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
  await page.getByRole('button', { name: '同步', exact: true }).click();
  await expect(page.getByRole('button', { name: '等待同步', exact: true })).toBeDisabled();
  jobs[primary][0].status = 'failed'; jobs[primary][0].error = 'HTTP 403：来源访问受限';
  await expect(page.locator('.source-card')).toContainText('同步失败', { timeout: 7000 });
  await expect(page.locator('.failure')).toContainText('HTTP 403');
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ user: primary, path: '/api/sources/sync', body: { source_id: 'source-one' } });
});

test('switch to an empty source space never shows a delayed old identity response', async ({ page }) => {
  await sourcesApp(page);
  let oldRequest = false;
  await page.route('**/api/sources', async route => {
    if (route.request().headers()['x-tiris-user'] === other) { await route.fulfill({ json: [] }); return; }
    oldRequest = true;
    await new Promise(resolve => setTimeout(resolve, 600));
    try { await route.fulfill({ json: [{ id: 'late-source', name: '旧身份的迟到来源', kind: 'rss', url: 'https://example.com/rss', enabled: true, last_success_at: null, last_error: null }] }); } catch { /* Canceled by identity switch. */ }
  });
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect.poll(() => oldRequest).toBe(true);
  await page.getByLabel('切换身份', { exact: true }).selectOption(other);
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.getByText('当前身份还没有来源。', { exact: true })).toBeVisible();
  await page.waitForTimeout(700);
  await expect(page.locator('.source-card')).toHaveCount(0);
  await expect(page.locator('.sources-view')).not.toContainText('旧身份的迟到来源');
});

test('form discovers available connector types from the unified backend catalog', async ({ page }) => {
  const { writes } = await sourcesApp(page);
  await page.route('**/api/source-types', route => route.fulfill({ json: [...sourceTypes, { kind: 'test_feed', label: '测试新平台', hint: '来自统一登记表', placeholder: 'https://example.com/test-feed' }] }));
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
  await page.getByLabel('来源类型', { exact: true }).selectOption('test_feed');
  await expect(page.getByText('来自统一登记表', { exact: true })).toBeVisible();
  await page.getByLabel('来源名称', { exact: true }).fill('新平台');
  await page.getByLabel('来源地址', { exact: true }).fill('https://example.com/test-feed');
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已添加到当前身份');
  expect(writes[0].body.kind).toBe('test_feed');
});
