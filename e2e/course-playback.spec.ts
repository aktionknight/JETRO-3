import { test, expect } from '@playwright/test';
import { courseFixture, mockLearning } from './learning-fixtures';

test('course polling ignores rotating playback tokens but reloads a different recording', async ({ page }) => {
  await mockLearning(page, false);
  let path = '/api/v1/media/recording_one/stream';
  let courseCalls = 0;
  // The backend signs a fresh playback token on every course fetch.
  await page.route('**/api/v1/learning/course', route => { courseCalls++; return route.fulfill({ json: { ...courseFixture, video_url: `${path}?token=${courseCalls}` } }); });
  await page.route('**/api/v1/media/*/stream?*', route => route.fulfill({
    path: 'e2e/fixtures/lesson.webm', contentType: 'video/webm',
  }));
  await page.route('**/api/v1/learning/playback', route => route.fulfill({
    json: {
      session_id: 'playback_test', position_seconds: 0,
      progress: { course_id: 'ball-bearing', release_id: 'release_001', watched_seconds: 0, course_completed: false, playground_unlocked: false, interview_unlocked: false, playground_result: null, interview: null },
    }
  }));
  await page.goto('/');
  await page.getByRole('navigation', { name: 'Course modules' }).getByRole('button', { name: 'Course content', exact: true }).click();
  const video = page.locator('video');
  await expect(video).toBeVisible();
  await video.evaluate((element: HTMLVideoElement) => { element.dataset.mounted = 'original'; element.muted = true; });
  await video.evaluate((element: HTMLVideoElement) => element.play().catch((error: DOMException) => {
    // The player pauses the first play request while opening its server watch session.
    if (error.name !== 'AbortError') throw error;
  }));
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => !element.paused && element.currentTime > 0.3)).toBe(true);
  const before = await video.evaluate((element: HTMLVideoElement) => element.currentTime);

  for (let poll = 0; poll < 2; poll++) {
    const calls = courseCalls;
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect.poll(() => courseCalls).toBeGreaterThan(calls);
  }
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBeGreaterThan(before + 0.5);
  expect(await video.evaluate((element: HTMLVideoElement) => ({ mounted: element.dataset.mounted, paused: element.paused })))
    .toEqual({ mounted: 'original', paused: false });

  path = '/api/v1/media/recording_two/stream';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.dataset.mounted ?? null)).toBeNull();
  await expect(video).toHaveAttribute('src', /recording_two\/stream/);
});
