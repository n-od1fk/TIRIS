import { expect, test } from '@playwright/test';
import { mockApp, other, primary, sourceTypes } from './fixtures';

const platform = { kind: 'github', title: 'GitHub 平台 API', url: 'https://github.com/', description: '不绑定单个仓库', credential_help_url: 'https://github.com/settings/personal-access-tokens', credential_fields: [{ name: 'token', label: 'GitHub Token', required: false, help: '公开读取可留空' }] };
const candidate = { id: 'platform-candidate', title: 'GitHub 平台 API', url: platform.url, access_type: 'platform_api', verified: true, error: '', method: 'platform_api', preview: [{ title: 'fixture/repository', url: 'https://github.com/fixture/repository', author: 'fixture', published_at: null, clean_text: 'Actual public metadata, not full README' }] };

async function setup(page: import('@playwright/test').Page, existing = false) {
  await mockApp(page);
  const source = { id: 'platform-one', name: 'GitHub', kind: 'github', url: platform.url, input_url: platform.url, access_type: 'platform_api', sync_supported: false, enabled: true, credential_configured: true, credential_mode: 'stored', connection_version: 1, last_success_at: null, last_error: null };
  const state = { sources: existing ? [source] : [], fail: false, writes: [] as any[], configured: existing };
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const body = request.postData() ? request.postDataJSON() : null;
    const user = request.headers()['x-tiris-user'];
    if (path === '/api/source-types') { await route.fulfill({ json: sourceTypes.map(row => ({ ...row, platform_tools: true })) }); return; }
    if (path === '/api/sources' && request.method() === 'GET') { await route.fulfill({ json: user === other ? [] : state.sources }); return; }
    if (path === '/api/jobs') { await route.fulfill({ json: [] }); return; }
    if (path === '/api/sources/discover') {
      state.writes.push({ path, body, user });
      await route.fulfill({ json: { discovery_id: 'recognized', platform, candidates: [], warnings: [], needs: [], message: '已识别平台 API，尚未验证保存' } }); return;
    }
    if (path === '/api/sources/verify-platform') {
      state.writes.push({ path, body, user });
      if (state.fail) { await route.fulfill({ status: 422, json: { detail: '平台连接验证失败：HTTP 401；未保存来源或更改已有凭证' } }); return; }
      state.configured = body.credential_action === 'keep' ? existing : body.credential_action === 'replace';
      await route.fulfill({ json: { discovery_id: 'verified', platform, candidates: [candidate], warnings: [], needs: [], message: '平台已验证，预览是公共仓库元数据' } }); return;
    }
    if (path === '/api/sources/connect') {
      state.writes.push({ path, body, user });
      state.sources = [{ ...source, credential_configured: state.configured, credential_mode: state.configured ? 'stored' : 'anonymous', connection_version: 2 }];
      await route.fulfill({ status: 201, json: { items: [{ source: state.sources[0], fetched_count: 1 }], auto_favorited: false } }); return;
    }
    await route.fallback();
  });
  await page.goto('/'); await page.getByRole('button', { name: '来源', exact: true }).click();
  return state;
}

test('platform root shows secret fields, validates, previews and saves through unified flow', async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel('来源地址', { exact: true }).fill(platform.url);
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  const token = page.getByLabel('GitHub Token', { exact: true });
  await expect(token).toHaveAttribute('type', 'password');
  await expect(page.getByRole('button', { name: /验证并保存所选来源/ })).toBeDisabled();
  await token.fill('fixture-secret-not-real');
  await page.getByRole('button', { name: '验证平台连接', exact: true }).click();
  await expect(token).toHaveValue('');
  await expect(page.getByText('fixture/repository', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '验证平台连接', exact: true })).toBeDisabled();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('fixture-secret-not-real');
  await page.screenshot({ path: 'test-results/platform-credential-preview.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: /验证并保存所选来源/ }).click();
  await expect(page.locator('.source-card')).toContainText('平台 API 连接');
  await expect(page.locator('.source-card')).toContainText('已配置凭证');
  await expect(page.locator('.source-card').getByRole('button', { name: '平台工具', exact: true })).toBeVisible();
  expect(state.writes[1]).toMatchObject({ path: '/api/sources/verify-platform', user: primary, body: { credentials: { token: 'fixture-secret-not-real' }, credential_action: 'replace' } });
  expect(JSON.stringify(state.writes[2])).not.toContain('fixture-secret-not-real');
  expect(state.writes[2].body.discovery_id).toBe('verified');
});

test('failed credentials cannot save and identity switch clears in-memory secrets', async ({ page }) => {
  const state = await setup(page); state.fail = true;
  await page.getByLabel('来源地址', { exact: true }).fill(platform.url);
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  await page.getByLabel('GitHub Token', { exact: true }).fill('invalid-fixture-secret');
  await page.getByRole('button', { name: '验证平台连接', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('HTTP 401');
  await expect(page.getByRole('alert')).not.toContainText('invalid-fixture-secret');
  await expect(page.getByRole('button', { name: /验证并保存所选来源/ })).toBeDisabled();
  expect(state.sources).toHaveLength(0);
  await page.getByLabel('切换身份', { exact: true }).selectOption(other);
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.getByLabel('GitHub Token', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('来源地址', { exact: true })).toHaveValue('');
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('invalid-fixture-secret');
});

test('editing never echoes token, supports keeping it or removing it explicitly', async ({ page }) => {
  const state = await setup(page, true);
  await page.getByRole('button', { name: '编辑来源', exact: true }).click();
  await page.getByRole('button', { name: '检测接入方式', exact: true }).click();
  const token = page.getByLabel('GitHub Token', { exact: true });
  await expect(token).toHaveValue('');
  await expect(page.getByText('此来源已有凭证，留空保留；输入新值会替换。页面不会回显已有 Token。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '验证平台连接', exact: true }).click();
  await expect(page.getByRole('button', { name: '验证并保存修改', exact: true })).toBeEnabled();
  expect(state.writes.at(-1).body).toMatchObject({ credentials: {}, credential_action: 'keep', edit_source_id: 'platform-one', expected_version: 1 });
  await page.getByLabel('移除凭证，改用匿名读取', { exact: true }).check();
  await expect(token).toBeDisabled();
  await expect(page.getByRole('button', { name: '验证并保存修改', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '验证平台连接', exact: true }).click();
  expect(state.writes.at(-1).body.credential_action).toBe('clear');
  await page.getByRole('button', { name: '验证并保存修改', exact: true }).click();
  await expect(page.locator('.source-card')).toContainText('匿名公开读取');
  await expect(page.getByLabel('GitHub Token', { exact: true })).toHaveCount(0);
});
