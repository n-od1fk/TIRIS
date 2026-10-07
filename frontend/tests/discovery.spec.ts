import { expect, test } from '@playwright/test';
import { mockApp, other } from './fixtures';

test('opens directly on a compact feed with four sidebar destinations', async ({ page }) => {
  const state = await mockApp(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今日推荐', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/compact-feed-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/compact-feed-mobile.png' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button')).toHaveCount(4);
  await expect(page.locator('.intro, .preference-intro')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '临时候选与来源' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '登录', exact: true })).toHaveCount(0);
  expect(state.requests.filter(request => request.path === '/api/sources')).toHaveLength(0);
  await page.getByRole('button', { name: '技术资讯 1', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('原推荐版本正文');
  await expect(page.getByRole('dialog')).not.toContainText('后来更新的正文');
});

test('scrolling loads beyond a single ten-item recommendation batch', async ({ page }) => {
  const state = await mockApp(page, { pages: 3 });
  await page.goto('/');
  await expect(page.locator('.feed .item')).toHaveCount(20);
  await page.locator('.feed-end').scrollIntoViewIfNeeded();
  await expect(page.locator('.feed .item')).toHaveCount(40);
  await page.locator('.feed-end').scrollIntoViewIfNeeded();
  await expect(page.locator('.feed .item')).toHaveCount(60);
  await expect.poll(() => state.requests.filter(request => request.path === '/api/feed/refill').length).toBe(1);
  await page.waitForTimeout(250);
  expect(state.requests.filter(request => request.path === '/api/feed/refill')).toHaveLength(1);
});

test('switching identity cancels delayed old pages and refreshes real request scope', async ({ page }) => {
  const state = await mockApp(page, { pages: 2, delayed: true });
  await page.goto('/');
  await expect(page.locator('.feed .item')).toHaveCount(20);
  await page.locator('.feed-end').scrollIntoViewIfNeeded();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('当前身份', { exact: true }).selectOption(other);
  await expect(page.getByRole('button', { name: '另一个身份的 AI 资讯', exact: true })).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByRole('button', { name: '技术资讯 21', exact: true })).toHaveCount(0);
  expect(state.requests.some(request => request.path === '/api/feed' && request.user === other)).toBe(true);
  await page.reload();
  await expect(page.getByRole('button', { name: '另一个身份的 AI 资讯', exact: true })).toBeVisible();
});

test('empty interests provide only a short settings prompt', async ({ page }) => {
  await mockApp(page, { needsProfile: true });
  await page.goto('/');
  await page.getByRole('button', { name: '填写画像', exact: true }).click();
  await expect(page.getByRole('heading', { name: '设置', exact: true })).toBeVisible();
  await expect(page.getByLabel('关注方向', { exact: true })).toBeVisible();
});
