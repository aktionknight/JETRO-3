import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  workers: 1,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:5174',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1440, height: 1000 },
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
      args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
    },
  },
  webServer: [
    {
      command: 'uv run uvicorn app.main:app --host 127.0.0.1 --port 8002 --no-access-log',
      cwd: './takumi-backend',
      url: 'http://127.0.0.1:8002/api/v1/health',
      env: {
        APP_ENV: 'test', AI_PROVIDER: 'fixture', GEMINI_API_KEY: '', GEMINI_MODEL: '', ENABLE_LIVE_EXTRACTION: 'false',
        ENABLE_LIVE_GAP_GENERATION: 'false', ENABLE_LIVE_QA: 'false',
        DEMO_SEED_STATE: 'approved_baseline',
        DATABASE_URL: `sqlite:///../test-results/learning-${Date.now()}.db`,
        COURSE_VIDEO_URL: 'http://127.0.0.1:5174/e2e/fixtures/lesson.webm',
        COURSE_VIDEO_DURATION_SECONDS: '6',
      },
      timeout: 60_000,
      reuseExistingServer: false,
    },
    {
      command: 'npm run dev:frontend -- --port 5174 --strictPort',
      url: 'http://127.0.0.1:5174',
      env: { VITE_API_TARGET: 'http://127.0.0.1:8002' },
      timeout: 30_000,
      reuseExistingServer: false,
    },
  ],
});
