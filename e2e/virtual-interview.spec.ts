import { test, expect, type Page, type WebSocketRoute } from '@playwright/test';
import { mockLearning } from './learning-fixtures';

// Device doubles expose callbacks so tests can deliver events after teardown.
export function installDevices(options: { missingRecognition?: boolean } = {}) {
  const device = {
    starts: 0, sources: [] as { finish: () => void; sample: number }[],
    recognizers: [] as Recognition[], suspended: false, resume: () => { },
    audioFailure: '', recognitionFailure: false,
    contexts: [] as Audio[], resumes: [] as { resolve: () => void; reject: () => void }[],
  };
  class Recognition {
    onresult: ((event: unknown) => void) | null = null;
    onend: (() => void) | null = null;
    onerror: ((event: { error: string }) => void) | null = null;
    constructor() { device.recognizers.push(this); }
    start() {
      device.starts++;
      if (device.recognitionFailure) throw new Error('Recognition temporarily unavailable');
    }
    abort() { }
  }
  class Audio {
    state = device.suspended ? 'suspended' : 'running';
    destination = {};
    constructor() {
      if (device.audioFailure === 'constructor') throw new Error('AudioContext unavailable');
      device.contexts.push(this);
    }
    resume() {
      if (this.state !== 'suspended') return Promise.resolve();
      return new Promise<void>((resolve, reject) => {
        device.resume = () => { this.state = 'running'; resolve(); };
        device.resumes.push({ resolve: device.resume, reject: () => reject(new Error('Audio output blocked')) });
      });
    }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createBuffer(_channels: number, length: number) {
      if (device.audioFailure === 'buffer') throw new Error('AudioBuffer unavailable');
      const data = new Float32Array(length);
      return { getChannelData: () => data };
    }
    createBufferSource() {
      const source = {
        buffer: null as { getChannelData: () => Float32Array } | null,
        onended: null as (() => void) | null,
        connect() { }, stop() { },
        start() { device.sources.push({ finish: () => source.onended?.(), sample: source.buffer!.getChannelData()[0] }); },
      };
      return source;
    }
  }
  Object.assign(window, {
    SpeechRecognition: options.missingRecognition ? undefined : Recognition,
    webkitSpeechRecognition: undefined,
    AudioContext: Audio,
    interviewDevice: device,
  });
}

declare global {
  interface Window {
    interviewDevice: {
      starts: number; suspended: boolean; resume: () => void; audioFailure: string; recognitionFailure: boolean;
      contexts: { state: string }[]; resumes: { resolve: () => void; reject: () => void }[];
      sources: { finish: () => void; sample: number }[];
      recognizers: { onresult: ((event: unknown) => void) | null; onend: (() => void) | null; onerror: ((event: { error: string }) => void) | null }[];
    };
    staleCapture?: { end: (() => void) | null; result: ((event: unknown) => void) | null };
  }
}

async function openInterview(page: Page, options: { suspended?: boolean; audioFailure?: string; missingRecognition?: boolean } = {}) {
  await page.addInitScript(installDevices, options);
  await mockLearning(page);
  let socket!: WebSocketRoute;
  const turns: { type: string; text?: string }[] = [];
  await page.routeWebSocket('**/api/v1/learning/interview/live?*', ws => {
    socket = ws;
    ws.onMessage(data => turns.push(JSON.parse(String(data))));
    ws.send(JSON.stringify({ type: 'status', status: 'connected' }));
  });
  await page.goto('/');
  const modules = page.getByRole('navigation', { name: 'Course modules' });
  await modules.getByRole('button', { name: 'Simulation playground', exact: true }).click();
  await page.getByRole('button', { name: 'Mark section complete', exact: true }).click();
  await modules.getByRole('button', { name: 'Virtual interview', exact: true }).click();
  await page.getByRole('button', { name: 'Start voice viva' }).click();
  await page.evaluate(options => {
    window.interviewDevice.suspended = !!options.suspended;
    window.interviewDevice.audioFailure = options.audioFailure ?? '';
  }, options);
  await page.getByRole('button', { name: 'Start Live Interview', exact: true }).click();
  await expect(page.locator('.vi-status-text')).toHaveText('Connected');
  await page.clock.install();
  await page.clock.pauseAt(new Date(Date.now() + 1000));
  return { turns, send: (message: object) => socket.send(JSON.stringify(message)), close: () => socket.close() };
}

async function speech(page: Page, phrases: string[], resultIndex = 0, final = true) {
  await page.evaluate(({ phrases, resultIndex, final }) => {
    window.interviewDevice.recognizers.at(-1)!.onresult?.({
      resultIndex,
      results: phrases.map(transcript => ({ isFinal: final, 0: { transcript } }))
    });
  }, { phrases, resultIndex, final });
}

for (const suspended of [false, true]) {
  test(`playback blocks capture and preserves FIFO while resume suspended=${suspended}`, async ({ page }) => {
    const interview = await openInterview(page);
    await page.evaluate(value => {
      window.interviewDevice.contexts.at(-1)!.state = value ? 'suspended' : 'running';
    }, suspended);
    interview.send({ type: 'audio', data: 'AQAAAA==' });
    interview.send({ type: 'audio', data: 'AgAAAA==' });
    interview.send({ type: 'status', status: 'listening' });
    await expect(page.locator('.vi-status-text')).toHaveText('Examiner speaking…');
    expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(0);
    if (suspended) {
      expect(await page.evaluate(() => window.interviewDevice.sources.length)).toBe(0);
      await page.evaluate(() => window.interviewDevice.resume());
    }
    await expect.poll(() => page.evaluate(() => window.interviewDevice.sources.length)).toBe(1);
    await page.evaluate(() => window.interviewDevice.sources[0].finish());
    await expect.poll(() => page.evaluate(() => window.interviewDevice.sources.length)).toBe(2);
    expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(0);
    expect(await page.evaluate(() => window.interviewDevice.sources.map(source => source.sample))).toEqual([1 / 32768, 2 / 32768]);
    await page.evaluate(() => window.interviewDevice.sources[1].finish());
    await expect(page.locator('.vi-status-text')).toHaveText('Listening to you (speak now)');
    expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  });
}

test('all finalized phrases survive advancing results and natural restart', async ({ page }) => {
  const interview = await openInterview(page);
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  const first = 'I inspect the approved evidence.';
  const second = 'Then I record the measured clearance.';
  await speech(page, [first]);
  await speech(page, [first, 'Then I record'], 1, false);
  await speech(page, [first, second], 1);
  await speech(page, [first, second], 1);
  await page.evaluate(() => window.interviewDevice.recognizers.at(-1)!.onend?.());
  await speech(page, ['I stop when evidence is missing.']);
  await page.clock.runFor(1800);
  await expect.poll(() => interview.turns.filter(turn => turn.type === 'user_turn')).toEqual([
    { type: 'user_turn', text: `${first} ${second} I stop when evidence is missing.` },
  ]);
});

test('mute and unmount invalidate saved callbacks and cancel debounce', async ({ page }) => {
  const interview = await openInterview(page);
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  await speech(page, ['I inspect the approved evidence.']);
  await page.evaluate(() => {
    const capture = window.interviewDevice.recognizers.at(-1)!;
    window.staleCapture = { end: capture.onend, result: capture.onresult };
  });
  await page.getByTitle('Mute microphone', { exact: true }).click();
  await page.evaluate(() => { window.staleCapture!.end?.(); window.staleCapture!.result?.({ results: [] }); });
  await page.clock.runFor(2000);
  expect(interview.turns).toEqual([]);
  expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  await expect(page.locator('.vi-interim')).toContainText('I inspect the approved evidence.');
  await page.getByTitle('Unmute microphone', { exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(2);
  await speech(page, ['Then I record the measured clearance.']);
  await page.evaluate(() => {
    const capture = window.interviewDevice.recognizers.at(-1)!;
    window.staleCapture = { end: capture.onend, result: capture.onresult };
  });
  await page.getByRole('button', { name: 'Change format' }).click();
  await page.evaluate(() => { window.staleCapture!.end?.(); window.staleCapture!.result?.({ results: [] }); });
  await page.clock.runFor(2000);
  expect(interview.turns).toEqual([]);
  expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(2);
});

for (const terminal of ['end', 'evaluating', 'error', 'close']) {
  test(`late resume cannot resurrect capture after ${terminal}`, async ({ page }) => {
    const interview = await openInterview(page);
    await page.evaluate(() => { window.interviewDevice.contexts.at(-1)!.state = 'suspended'; });
    interview.send({ type: 'audio', data: 'AQAAAA==' });
    interview.send({ type: 'status', status: 'listening' });
    await expect(page.locator('.vi-status-text')).toHaveText('Examiner speaking…');
    if (terminal === 'end') await page.getByTitle('End interview', { exact: true }).click();
    else if (terminal === 'close') interview.close();
    else interview.send(terminal === 'error' ? { type: 'error', message: 'Unavailable' } : { type: 'status', status: terminal });
    await expect(page.locator('.vi-status-text')).not.toHaveText('Examiner speaking…');
    if (terminal !== 'close') interview.send({ type: 'status', status: 'listening' });
    await page.evaluate(() => window.interviewDevice.resume());
    expect(await page.evaluate(() => ({ starts: window.interviewDevice.starts, sources: window.interviewDevice.sources.length }))).toEqual({ starts: 0, sources: 0 });
  });
}

test('manual submission locks out debounce and denied recognition leaves keyboard usable', async ({ page }) => {
  const interview = await openInterview(page);
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  await speech(page, ['I inspect the approved evidence.']);
  await page.getByRole('button', { name: 'Done Speaking', exact: true }).click();
  await page.clock.runFor(1800);
  expect(interview.turns.filter(turn => turn.type === 'user_turn')).toHaveLength(1);
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(2);
  await page.evaluate(() => {
    const capture = window.interviewDevice.recognizers.at(-1)!;
    const ended = capture.onend; capture.onerror?.({ error: 'not-allowed' }); ended?.();
  });
  await expect(page.locator('.vi-error')).toContainText('Microphone permission was denied.');
  await page.locator('.vi-text-field').fill('I use the approved release only.');
  await page.getByTitle('Submit answer', { exact: true }).click();
  await expect.poll(() => interview.turns.filter(turn => turn.type === 'user_turn').map(turn => turn.text)).toEqual([
    'I inspect the approved evidence.', 'I use the approved release only.',
  ]);
});

test('failed send retains draft for retry', async ({ page }) => {
  const interview = await openInterview(page);
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  await speech(page, ['I inspect the approved evidence.'], 0, false);
  await page.evaluate(() => {
    const original = WebSocket.prototype.send;
    WebSocket.prototype.send = function(data) { WebSocket.prototype.send = original; throw new Error('transport failed'); };
  });
  await page.getByRole('button', { name: 'Done Speaking', exact: true }).click();
  await expect(page.locator('.vi-interim')).toContainText('I inspect the approved evidence.');
  expect(interview.turns).toEqual([]);
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(2);
  await speech(page, ['Then I record the measured clearance.']);
  await page.clock.runFor(1800);
  await expect.poll(() => interview.turns.filter(turn => turn.type === 'user_turn')).toEqual([
    { type: 'user_turn', text: 'I inspect the approved evidence. Then I record the measured clearance.' },
  ]);
});

for (const failure of ['constructor', 'buffer']) {
  test(`capture resumes after synchronous audio ${failure} failure while listening`, async ({ page }) => {
    const interview = await openInterview(page);
    interview.send({ type: 'status', status: 'listening' });
    await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
    await page.evaluate(value => {
      window.interviewDevice.audioFailure = value;
      if (value === 'constructor') window.interviewDevice.contexts.at(-1)!.state = 'closed';
    }, failure);
    interview.send({ type: 'audio', data: 'AQAAAA==' });
    await expect(page.locator('.vi-status-text')).toHaveText('Listening to you (speak now)');
    await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(2);
    await speech(page, ['I inspect the approved evidence.']);
    await page.clock.runFor(1800);
    await expect.poll(() => interview.turns.filter(turn => turn.type === 'user_turn')).toEqual([
      { type: 'user_turn', text: 'I inspect the approved evidence.' },
    ]);
  });
}

test('voice_error keeps examiner text and keyboard submission usable', async ({ page }) => {
  const interview = await openInterview(page);
  interview.send({ type: 'transcript', role: 'model', text: 'Which evidence do you inspect first?' });
  interview.send({ type: 'voice_error', message: 'Examiner voice is unavailable. Read the question and type your answer.' });
  interview.send({ type: 'status', status: 'listening' });
  await expect(page.locator('.vi-error')).toHaveText('Examiner voice is unavailable. Read the question and type your answer.');
  await expect(page.locator('.vi-live-transcript')).toContainText('Which evidence do you inspect first?');
  await page.locator('.vi-text-field').fill('I inspect the approved evidence.');
  await page.getByTitle('Submit answer', { exact: true }).click();
  await expect.poll(() => interview.turns).toEqual([{ type: 'user_turn', text: 'I inspect the approved evidence.' }]);
});

for (const failure of ['audio-capture', 'network']) {
  test(`recognition ${failure} stops bounded retries and keeps keyboard usable`, async ({ page }) => {
    const interview = await openInterview(page);
    interview.send({ type: 'status', status: 'listening' });
    await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
    await page.evaluate(failure => {
      const capture = window.interviewDevice.recognizers.at(-1)!;
      const ended = capture.onend;
      const error = capture.onerror;
      for (let i = 0; i < 3; i++) error?.({ error: failure });
      ended?.();
    }, failure);
    await page.clock.runFor(2000);
    expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(1);
    await expect(page.locator('.vi-error')).toContainText(failure === 'audio-capture'
      ? 'No microphone input is available.' : 'Speech recognition is unavailable.');
    await page.locator('.vi-text-field').fill('I inspect the approved evidence.');
    await page.getByTitle('Submit answer', { exact: true }).click();
    await expect.poll(() => interview.turns).toEqual([{ type: 'user_turn', text: 'I inspect the approved evidence.' }]);
  });
}

test('missing recognition and gesture-time audio failure leave text interviewing usable', async ({ page }) => {
  const interview = await openInterview(page, { missingRecognition: true, audioFailure: 'constructor' });
  interview.send({ type: 'status', status: 'listening' });
  await expect(page.locator('.vi-error')).toContainText('Voice recognition is unavailable in this browser.');
  await page.locator('.vi-text-field').fill('I inspect the approved evidence.');
  await page.getByTitle('Submit answer', { exact: true }).click();
  await expect.poll(() => interview.turns).toEqual([{ type: 'user_turn', text: 'I inspect the approved evidence.' }]);
});

for (const terminal of ['end', 'unmount', 'new-session']) {
  test(`late gesture unlock cannot affect ${terminal}`, async ({ page }) => {
    const interview = await openInterview(page, { suspended: true });
    expect(await page.evaluate(() => window.interviewDevice.resumes.length)).toBe(1);
    if (terminal === 'end') await page.getByTitle('End interview', { exact: true }).click();
    else if (terminal === 'unmount') await page.getByRole('button', { name: 'Change format' }).click();
    else {
      interview.send({ type: 'status', status: 'ended', duration: 1 });
      await page.getByRole('button', { name: 'Start voice viva' }).click();
      await page.getByRole('button', { name: 'Start Live Interview', exact: true }).click();
      await expect(page.locator('.vi-status-text')).toHaveText('Connected');
    }
    await page.evaluate(() => window.interviewDevice.resumes[0].reject());
    await page.clock.runFor(2000);
    expect(await page.evaluate(() => window.interviewDevice.contexts[0].state)).toBe('closed');
    expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(0);
    await expect(page.locator('.vi-error')).toHaveCount(0);
    if (terminal === 'end') await expect(page.locator('.vi-status-text')).toHaveText('Preparing and saving your evaluation report…');
    if (terminal === 'new-session') await expect(page.locator('.vi-status-text')).toHaveText('Connected');
  });
}

test('recognizer start failures stop after three attempts and allow explicit retry', async ({ page }) => {
  const interview = await openInterview(page);
  await page.evaluate(() => { window.interviewDevice.recognitionFailure = true; });
  interview.send({ type: 'status', status: 'listening' });
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(1);
  await page.clock.runFor(1000);
  expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(3);
  await expect(page.locator('.vi-error')).toContainText('Microphone could not restart.');
  await page.clock.runFor(2000);
  expect(await page.evaluate(() => window.interviewDevice.starts)).toBe(3);
  await page.getByTitle('Mute microphone', { exact: true }).click();
  await page.evaluate(() => { window.interviewDevice.recognitionFailure = false; });
  await page.getByTitle('Unmute microphone', { exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.interviewDevice.starts)).toBe(4);
  await speech(page, ['I inspect the approved evidence.']);
  await page.clock.runFor(1800);
  await expect.poll(() => interview.turns).toEqual([{ type: 'user_turn', text: 'I inspect the approved evidence.' }]);
});
