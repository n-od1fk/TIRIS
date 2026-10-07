// Read-only smoke against live services: suppress automatic refill to avoid personal discovery.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';

const base = new URL(process.env.TIRIS_BASE_URL || 'http://127.0.0.1:5173');
assert(['127.0.0.1', 'localhost'].includes(base.hostname), 'Use a local TIRIS instance only');
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/feed/refill', route => route.fulfill({ status: 202, json: { state: 'needs_profile', run: null, retry_after: 60 } }));
  const candidates = await page.request.get(new URL('/api/items', base).href);
  assert.equal(candidates.status(), 200);
  const data = await candidates.json();
  const profile = await (await page.request.get(new URL('/api/profile', base).href)).json();
  const sources = await (await page.request.get(new URL('/api/sources', base).href)).json();
  await page.goto(base.href);
  await expect(page.getByRole('heading', { name: '今日推荐', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button')).toHaveCount(4);
  await mkdir('test-results', { recursive: true });
  await page.screenshot({ path: 'test-results/live-compact-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile feed overflow');
  await page.screenshot({ path: 'test-results/live-compact-mobile.png' });
  await page.getByRole('button', { name: '来源', exact: true }).click();
  await expect(page.getByRole('heading', { name: '来源', exact: true })).toBeVisible();
  await expect(page.getByLabel('来源类型', { exact: true })).toBeVisible();
  await expect(page.locator('.source-card')).toHaveCount(sources.length);
  if (!sources.length) await expect(page.getByText('当前身份还没有来源。', { exact: true })).toBeVisible();
  await expect(page.locator('.sources-view [role="alert"]')).toHaveCount(0);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile sources overflow');
  await page.screenshot({ path: 'test-results/live-sources-mobile.png' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: 'test-results/live-sources-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await expect(page.getByLabel('专业与背景', { exact: true })).toHaveValue(profile.data.background);
  await expect(page.getByLabel('当前身份', { exact: true })).toBeVisible();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile settings overflow');
  await page.screenshot({ path: 'test-results/live-settings-mobile.png' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: '暗色', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: 'test-results/live-settings-dark.png' });
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.getByRole('heading', { name: '资料库', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '更新提醒', exact: true }).click();
  await expect(page.getByRole('heading', { name: '收藏更新提醒', exact: true })).toBeVisible();
  const identities = await (await page.request.get(new URL('/api/identities', base).href)).json();
  const sourceCounts = [];
  for (const identity of identities.items) {
    const headers = { 'X-TIRIS-User': identity.id };
    const expectedSources = await (await page.request.get(new URL('/api/sources', base).href, { headers })).json();
    const expectedLibrary = await (await page.request.get(new URL('/api/items?scope=library', base).href, { headers })).json();
    await page.getByLabel('切换身份', { exact: true }).selectOption(identity.id);
    await page.getByRole('button', { name: '资料库', exact: true }).click();
    // The current identity may already be on library/updates; choose saved materials.
    await page.getByRole('button', { name: '收藏', exact: true }).first().click();
    await expect(page.locator('.main-content > section:not([hidden]) .item')).toHaveCount(expectedLibrary.items.length);
    await page.getByRole('button', { name: '来源', exact: true }).click();
    await expect(page.getByLabel('来源类型', { exact: true })).toBeEnabled();
    await expect(page.locator('.source-card')).toHaveCount(expectedSources.length);
    if (!expectedSources.length) await expect(page.getByText('当前身份还没有来源。', { exact: true })).toBeVisible();
    for (const source of expectedSources) await expect(page.locator('.source-card').filter({ has: page.getByRole('heading', { name: source.name, exact: true }) })).toBeVisible();
    sourceCounts.push(expectedSources.length);
    await page.screenshot({ path: `test-results/live-identity-sources-${identity.id}.png` });
  }
  const after = await (await page.request.get(new URL('/api/profile', base).href)).json();
  assert.equal(after.version, profile.version);
  assert.deepEqual(errors, []);
  console.log(`Live compact UI smoke passed: four destinations, identity selector, sources, profile, theme, library and updates; desktop/mobile; ${data.total} candidates preserved; per-identity visible source counts ${sourceCounts.join('/')}; refill suppressed; no server writes or paid calls`);
} finally { await browser.close(); }
