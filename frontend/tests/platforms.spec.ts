import { expect, test } from '@playwright/test';
import { mockApp, primary, sourceTypes } from './fixtures';

test('platform connection reads full body and downloads original with identity header', async ({ page }) => {
  await mockApp(page);
  const writes: { path: string; body: any; user: string }[] = [];
  const source = { id: 'github-platform', kind: 'github', name: 'GitHub 平台', url: 'https://github.com/', enabled: true, last_success_at: null, last_error: null };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/sources') { await route.fulfill({ json: [source] }); return; }
    if (path === '/api/source-types') { await route.fulfill({ json: sourceTypes.map(t => ({ ...t, platform_tools: true })) }); return; }
    if (path === '/api/platforms/github-platform/capabilities') { await route.fulfill({ json: { operations: ['list', 'search', 'stars', 'releases'], credential_env: 'GITHUB_TOKEN', credential_configured: false } }); return; }
    if (path === '/api/platforms/github-platform/query') {
      writes.push({ path, body: request.postDataJSON(), user: request.headers()['x-tiris-user'] });
      await route.fulfill({ json: { items: [{ title: '任意发现的仓库', url: 'https://github.com/other/repo', acquisition_level: 'metadata_only' }], next_cursor: null, scope: '平台查询' } }); return;
    }
    if (path === '/api/platforms/github-platform/detail') {
      writes.push({ path, body: request.postDataJSON(), user: request.headers()['x-tiris-user'] });
      await route.fulfill({ json: { item_id: 'platform-item', title: '完整资料', original_url: 'https://github.com/other/repo', body: '完整 README 正文包含代码与实践说明', status: 'full', reason: '', materials: [{ id: 'raw-one', filename: 'README.md', download_url: '/api/platforms/materials/raw-one' }], attachments: [] } }); return;
    }
    if (path === '/api/platforms/materials/raw-one') {
      writes.push({ path, body: null, user: request.headers()['x-tiris-user'] });
      await route.fulfill({ contentType: 'application/octet-stream', body: '# Original README' }); return;
    }
    if (path === '/api/items/platform-item/favorite') {
      writes.push({ path, body: request.postDataJSON(), user: request.headers()['x-tiris-user'] });
      await route.fulfill({ json: { favorite: true } }); return;
    }
    await route.fallback();
  });
  await page.goto('/');
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await page.getByRole('button', { name: '平台工具', exact: true }).click();
  await expect(page.getByRole('button', { name: '同步', exact: true })).toHaveCount(0);
  await page.getByLabel('平台操作').selectOption('search');
  await page.getByLabel('查询条件').fill('security');
  await page.getByRole('button', { name: '获取列表', exact: true }).click();
  await page.getByRole('button', { name: '读取正文与原文件', exact: true }).click();
  await expect(page.locator('.platform-body')).toContainText('完整 README 正文');
  await expect(page.getByRole('link', { name: '查看原文', exact: true })).toHaveAttribute('href', 'https://github.com/other/repo');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 README.md', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('README.md');
  expect(writes.every(w => w.user === primary)).toBe(true);
  expect(writes[0].body.query).toBe('security');
  expect(writes[1].body.url).toBe('https://github.com/other/repo');
  expect(writes.some(w => w.path.endsWith('/favorite'))).toBe(false);
  await page.getByRole('button', { name: '收藏到资料库', exact: true }).click();
  await expect(page.getByText('已收藏到当前身份的资料库。', { exact: true })).toBeVisible();
  expect(writes.at(-1)).toEqual({ path: '/api/items/platform-item/favorite', body: { enabled: true }, user: primary });
});
