import { expect, test } from '@playwright/test';
import { mockApp } from './fixtures';

test('only favorite enters library and updates belong inside library', async ({ page }) => {
  const state = await mockApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.getByText('还没有收藏。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '今日推荐', exact: true }).click();
  await page.locator('.feed .item').getByRole('button', { name: '收藏', exact: true }).click();
  await expect(page.locator('.feed .item').getByRole('button', { name: '已收藏', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '资料库', exact: true }).click();
  await expect(page.getByRole('button', { name: '技术资讯 1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '更新提醒', exact: true }).click();
  await expect(page.getByRole('heading', { name: '收藏更新提醒' })).toBeVisible();
  await expect(page.getByText('未读 1', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '标记已读', exact: true }).click();
  await expect(page.getByText('未读 0', { exact: true })).toBeVisible();
  expect(state.saved).toBe(true);
  await page.locator('.library-tabs').getByRole('button', { name: '收藏', exact: true }).click();
  await page.locator('article.item').getByRole('button', { name: '已收藏', exact: true }).click();
  await expect(page.getByText('还没有收藏。', { exact: true })).toBeVisible();
  expect(state.saved).toBe(false);
});
