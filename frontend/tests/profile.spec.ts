import { expect, test } from '@playwright/test';
import { mockApp } from './fixtures';

test('settings only shows appearance identity and profile; drafts and hidden fields survive', async ({ page }) => {
  const state = await mockApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await expect(page.locator('.settings-view > .settings-panel')).toHaveCount(3);
  await page.getByLabel('专业与背景', { exact: true }).fill('网安本科生');
  await page.getByLabel('关注方向', { exact: true }).fill('security\nAI');
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await expect(page.getByLabel('专业与背景', { exact: true })).toHaveValue('网安本科生');
  await page.getByRole('button', { name: '保存画像', exact: true }).click();
  await expect(page.getByText('已保存', { exact: true })).toBeVisible();
  const write = state.requests.find(request => request.path === '/api/profile' && request.method === 'PATCH');
  expect(write?.body).toMatchObject({ expected_version: 0, background: '网安本科生', long_term_directions: ['security', 'AI'], language_preferences: ['zh'], daily_reading_minutes: 30 });
  expect(state.requests.filter(request => request.path === '/api/fixed-columns')).toHaveLength(0);
  await page.getByRole('button', { name: '暗色', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: 'test-results/compact-settings-dark.png' });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('conflict keeps draft and does not automatically retry a save', async ({ page }) => {
  const state = await mockApp(page, { conflict: true });
  await page.goto('/');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('专业与背景', { exact: true }).fill('需要保留的草稿');
  await page.getByRole('button', { name: '保存画像', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('画像版本冲突');
  await expect(page.getByLabel('专业与背景', { exact: true })).toHaveValue('需要保留的草稿');
  expect(state.requests.filter(request => request.method === 'PATCH')).toHaveLength(1);
});

test('identity name can be saved and new identity added without login', async ({ page }) => {
  const state = await mockApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('身份名称', { exact: true }).fill('我的安全研究');
  await page.getByRole('button', { name: '保存名称', exact: true }).click();
  await expect(page.locator('.identity-label option:checked')).toHaveText('我的安全研究');
  await page.getByLabel('新身份', { exact: true }).fill('开发学习');
  await page.getByRole('button', { name: '添加并切换', exact: true }).click();
  await expect(page.locator('.identity-label option:checked')).toHaveText('开发学习');
  await expect(page.getByRole('heading', { name: '今日推荐', exact: true })).toBeVisible();
  expect(state.requests.some(request => request.path === '/api/identities' && request.method === 'POST')).toBe(true);
});
