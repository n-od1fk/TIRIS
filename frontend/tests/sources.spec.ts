import { expect, test } from '@playwright/test';
import { item, mockApp, other, primary } from './fixtures';

async function sourcesApp(page: import('@playwright/test').Page) {
  const state = await mockApp(page);
  const sources: Record<string, any[]> = {
    [primary]: [{ id: 'source-one', name: '安全博客', kind: 'rss', url: 'https://example.com/rss', input_url: 'https://example.com/', connection_version: 1, access_type: 'rss', enabled: true, last_success_at: null, last_error: null }],
    [other]: [{ id: 'source-two', name: 'AI 仓库', kind: 'github', url: 'https://github.com/example/ai', enabled: true, last_success_at: null, last_error: null }],
  };
  const jobs: Record<string, any[]> = { [primary]: [], [other]: [] };
  const writes: any[] = [];
  const candidates = [
    { id: 'candidate-rss', title: 'Public news', url: 'https://news.example/rss', access_type: 'rss', verified: true, error: '', method: 'html_link', preview: [{ title: 'Preview article', url: 'https://news.example/article', author: 'Alice', published_at: null, clean_text: 'Actual feed excerpt' }] },
    { id: 'candidate-json', title: 'JSON news', url: 'https://news.example/feed.json', access_type: 'json_feed', verified: true, error: '', method: 'html_link', preview: [] },
    { id: 'candidate-failed', title: 'Blocked', url: 'https://news.example/blocked', access_type: 'unknown', verified: false, error: 'HTTP 403', method: 'html_link', preview: [] },
  ];
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const user = request.headers()['x-tiris-user'] || primary;
    const method = request.method(); const body = request.postData() ? request.postDataJSON() : null;
    if (path === '/api/sources/discover') {
      writes.push({ user, path, body });
      await route.fulfill({ json: { discovery_id: 'draft-one', input_url: body.url, message: '请选择已验证订阅源保存', candidates, warnings: [], needs: [] } }); return;
    }
    if (path === '/api/sources/connect') {
      writes.push({ user, path, body });
      const rows = body.selections.map((choice: any, i: number) => {
        const candidate = candidates.find(row => row.id === choice.candidate_id)!;
        const row = { id: body.edit_source_id || `new-${sources[user].length + i}`, name: choice.name || candidate.title, kind: 'rss', url: candidate.url, access_type: candidate.access_type, enabled: body.enabled, connection_version: 2, last_success_at: null, last_error: null };
        if (body.edit_source_id) Object.assign(sources[user].find(source => source.id === body.edit_source_id), row);
        else sources[user].push(row);
        return { source: row, fetched_count: 1 };
      });
      await route.fulfill({ status: 201, json: { items: rows, auto_favorited: false } }); return;
    }
    if (path === '/api/sources/sync') {
      writes.push({ user, path, body });
      const job = { id: 'job-one', source_id: body.source_id, status: 'queued', error: null };
      jobs[user] = [job]; await route.fulfill({ status: 202, json: job }); return;
    }
    if (path.startsWith('/api/sources/') && method === 'PATCH') {
      writes.push({ user, path, body }); Object.assign(sources[user].find(source => source.id === path.split('/').at(-1)), body);
      await route.fulfill({ json: sources[user][0] }); return;
    }
    if (path.startsWith('/api/sources/') && method === 'DELETE') {
      writes.push({ user, path, body }); sources[user] = sources[user].filter(source => source.id !== path.split('/').at(-1));
      await route.fulfill({ json: { deleted: true, history_preserved: true } }); return;
    }
    if (path === '/api/sources') {
      if (method === 'POST') {
        writes.push({ user, path, body }); sources[user].push({ ...body, id: 'restored', enabled: true });
        await route.fulfill({ status: 201, json: sources[user].at(-1) }); return;
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

async function openSources(page: import('@playwright/test').Page) {
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
}

test('identity switch changes feed library sources and resets detection draft', async ({ page }) => {
  const { state } = await sourcesApp(page); state.saved = true;
  await openSources(page);
  await expect(page.locator('.source-card')).toContainText('安全博客');
  await page.getByLabel('来源地址', { exact: true }).fill('https://news.example/');
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await expect(page.getByLabel('接入识别结果')).toBeVisible();
  await page.getByLabel('切换身份', { exact: true }).selectOption(other);
  await expect(page.getByRole('button', { name: '另一个身份的 AI 资讯', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.locator('.main-content > section:not([hidden]) .item')).toContainText('AI 已收藏资料');
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.locator('.source-card')).toContainText('AI 仓库');
  await expect(page.getByLabel('接入识别结果')).toHaveCount(0);
  await expect(page.getByLabel('来源地址', { exact: true })).toHaveValue('');
  await page.getByLabel('切换身份', { exact: true }).selectOption(primary);
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toBeVisible();
});

test('URL-only discovery shows preview, multiple choices and verifies before save', async ({ page }) => {
  const { writes } = await sourcesApp(page); await openSources(page);
  await expect(page.getByLabel('来源类型', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('来源地址', { exact: true })).toHaveValue('');
  await page.getByLabel('来源地址', { exact: true }).fill('https://news.example/');
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  const save = page.getByRole('button', { name: /验证并保存所选来源/ });
  await expect(save).toBeDisabled();
  await expect(page.getByLabel('Blocked · 未通过验证')).toBeDisabled();
  await page.getByText('Preview article', { exact: true }).click();
  await expect(page.getByText('Actual feed excerpt', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '查看原文', exact: true })).toHaveAttribute('href', 'https://news.example/article');
  expect(writes).toHaveLength(1); // Discovery has not persisted a source.
  await page.getByLabel('Public news · RSS 已验证', { exact: true }).check();
  await page.getByLabel('JSON news · JSON Feed 已验证', { exact: true }).check();
  await save.click();
  await expect(page.getByRole('status')).toContainText('未自动收藏');
  await expect(page.locator('.source-card')).toHaveCount(3);
  expect(writes[1]).toMatchObject({ user: primary, path: '/api/sources/connect', body: { discovery_id: 'draft-one', selections: [{ candidate_id: 'candidate-rss', name: '' }, { candidate_id: 'candidate-json', name: '' }], enabled: true } });
  const card = page.locator('.source-card').filter({ has: page.getByRole('heading', { name: 'Public news', exact: true }) });
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByRole('button', { name: '同步', exact: true })).toBeDisabled();
  await card.getByRole('button', { name: '启用', exact: true }).click();
  await expect(card.getByRole('button', { name: '同步', exact: true })).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/sources-mobile.png' });
});

test('changing URL invalidates old detection and unsupported sites explain missing configuration', async ({ page }) => {
  await sourcesApp(page); await openSources(page);
  await page.getByLabel('来源地址', { exact: true }).fill('https://news.example/');
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await expect(page.getByLabel('接入识别结果')).toBeVisible();
  await page.getByLabel('来源地址', { exact: true }).fill('https://unsupported.example/');
  await expect(page.getByLabel('接入识别结果')).toHaveCount(0);
  await page.route('**/api/sources/discover', route => route.fulfill({ json: { discovery_id: 'failed', candidates: [], warnings: [], needs: ['请提供订阅地址或 API 文档及 API Key'], message: '识别为网页不等于接入成功' } }));
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await expect(page.getByText('请提供订阅地址或 API 文档及 API Key', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /验证并保存所选来源/ })).toBeDisabled();
});

test('edit can rename without networking or re-detect a changed connection with version guard', async ({ page }) => {
  const { writes } = await sourcesApp(page); await openSources(page);
  await page.getByRole('button', { name: '编辑来源', exact: true }).click();
  await expect(page.getByLabel('来源地址', { exact: true })).toHaveValue('https://example.com/');
  await page.getByLabel('来源名称', { exact: true }).fill('改名');
  await page.getByRole('button', { name: '仅保存名称', exact: true }).click();
  await expect(page.locator('.source-card')).toContainText('改名');
  expect(writes[0].body).toEqual({ name: '改名', expected_version: 1 });
  await page.getByRole('button', { name: '编辑来源', exact: true }).click();
  await page.getByLabel('来源地址', { exact: true }).fill('https://news.example/');
  await expect(page.getByRole('button', { name: '仅保存名称', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await page.getByLabel('Public news · RSS 已验证', { exact: true }).check();
  await page.getByLabel('JSON news · JSON Feed 已验证', { exact: true }).check();
  await expect(page.getByLabel('Public news · RSS 已验证', { exact: true })).not.toBeChecked();
  await page.getByRole('button', { name: '验证并保存修改', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('历史和收藏保留');
  expect(writes.at(-1).body).toMatchObject({ edit_source_id: 'source-one', expected_version: 1, selections: [{ candidate_id: 'candidate-json', name: '改名' }] });
});

test('save error preserves selected draft and optional name for retry', async ({ page }) => {
  await sourcesApp(page); await openSources(page);
  await page.getByLabel('来源地址', { exact: true }).fill('https://news.example/');
  await page.getByLabel('来源名称', { exact: true }).fill('保留名称');
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await page.getByLabel('Public news · RSS 已验证', { exact: true }).check();
  await page.route('**/api/sources/connect', route => route.fulfill({ status: 422, json: { detail: '保存前复验失败：HTTP 403；未更改来源配置' } }));
  await page.getByRole('button', { name: /验证并保存所选来源/ }).click();
  await expect(page.getByRole('alert')).toContainText('复验失败');
  await expect(page.getByLabel('Public news · RSS 已验证', { exact: true })).toBeChecked();
  await expect(page.getByLabel('来源名称', { exact: true })).toHaveValue('保留名称');
});

test('manual sync reports queued state then honest fetch failure', async ({ page }) => {
  const { jobs, writes } = await sourcesApp(page); await openSources(page);
  await page.getByRole('button', { name: '同步', exact: true }).click();
  await expect(page.getByRole('button', { name: '等待同步', exact: true })).toBeDisabled();
  jobs[primary][0].status = 'failed'; jobs[primary][0].error = 'HTTP 403：来源访问受限';
  await expect(page.locator('.source-card')).toContainText('同步失败', { timeout: 7000 });
  await expect(page.locator('.failure')).toContainText('HTTP 403');
  expect(writes).toHaveLength(1);
});

test('empty identity never shows a delayed response from another identity', async ({ page }) => {
  await sourcesApp(page); let oldRequest = false;
  await page.route('**/api/sources', async route => {
    if (route.request().headers()['x-tiris-user'] === other) { await route.fulfill({ json: [] }); return; }
    oldRequest = true; await new Promise(resolve => setTimeout(resolve, 600));
    try { await route.fulfill({ json: [{ id: 'late', name: '旧身份迟到来源', kind: 'rss', url: 'https://example.com/rss', enabled: true }] }); } catch { /* Request canceled. */ }
  });
  await openSources(page); await expect.poll(() => oldRequest).toBe(true);
  await page.getByLabel('切换身份', { exact: true }).selectOption(other);
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.getByText('当前身份还没有来源。', { exact: true })).toBeVisible();
  await page.waitForTimeout(700);
  await expect(page.locator('.source-card')).toHaveCount(0);
});

test('delete requires confirmation, preserves other identity and supports undo', async ({ page }) => {
  const { writes, sources } = await sourcesApp(page); await openSources(page);
  await page.getByRole('button', { name: '删除来源', exact: true }).click();
  await expect(page.getByText('删除“安全博客”？停止后续采集，保留已收藏资料和历史版本。', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认删除', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '删除来源', exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.locator('.source-card')).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已收藏资料与历史保留');
  expect(sources[other]).toHaveLength(1);
  await page.getByRole('button', { name: '撤销删除', exact: true }).click();
  await expect(page.locator('.source-card')).toHaveCount(1);
  await expect(page.getByRole('status')).toContainText('已恢复来源');
});
