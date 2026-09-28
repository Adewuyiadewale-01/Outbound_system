#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

function argValue(name, fallback = '') {
  const idx = process.argv.indexOf(name);
  if (idx === -1 || idx + 1 >= process.argv.length) return fallback;
  return process.argv[idx + 1];
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function jitter(minMs, maxMs) {
  return minMs + Math.floor(Math.random() * (maxMs - minMs + 1));
}

function robotRetryCount(task) {
  return Number(task?.robot_retry_count || 0);
}

function withRobotRetry(task) {
  return {
    ...task,
    robot_retry_count: robotRetryCount(task) + 1,
  };
}

function taskSummary(task) {
  return {
    lead_id: task.lead_id || '',
    company: task.company || '',
    person_name: task.person_name || '',
    query: task.query || '',
  };
}

function unwrapYahooUrl(rawUrl) {
  try {
    const decoded = decodeURIComponent(rawUrl);
    const match = decoded.match(/\/RU=([^/]+)\//);
    if (match) return decodeURIComponent(match[1]);
  } catch {
    // Keep raw URL.
  }
  return rawUrl;
}

async function yahooSearch(page, query, limit) {
  const url = `https://search.yahoo.com/search?p=${encodeURIComponent(query)}`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(1200);
  const results = await page.evaluate((maxResults) => {
    const rows = [];
    const items = Array.from(document.querySelectorAll('li div.algo'));
    for (const item of items) {
      const anchor = item.querySelector('a[href]');
      const titleEl = item.querySelector('h3');
      const snippetEl = item.querySelector('.compText p, p');
      const title = titleEl?.innerText?.trim() || anchor?.innerText?.trim() || '';
      const url = anchor?.href || '';
      const snippet = snippetEl?.innerText?.trim() || '';
      if (title && url) rows.push({ title, url, snippet });
      if (rows.length >= maxResults) break;
    }
    return rows;
  }, limit);
  return results.map((row) => ({ ...row, url: unwrapYahooUrl(row.url), engine: 'yahoo' }));
}

async function googleSearch(page, query, limit) {
  await page.goto('https://www.google.com/', { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(jitter(900, 1600));
  await handleGoogleConsent(page);
  await detectGoogleBlock(page);

  const searchBox = page.locator('textarea[name="q"], input[name="q"]').first();
  await searchBox.waitFor({ state: 'visible', timeout: 15000 });
  await searchBox.click({ delay: jitter(50, 120) });
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
  await page.waitForTimeout(jitter(150, 350));
  await searchBox.fill(query);
  await page.waitForTimeout(jitter(300, 900));
  await page.keyboard.press('Enter');
  await page.waitForLoadState('domcontentloaded', { timeout: 45000 }).catch(() => {});
  await page.waitForTimeout(jitter(1800, 3200));
  await detectGoogleBlock(page);

  const results = await page.evaluate((maxResults) => {
    const rows = [];
    const seen = new Set();
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    for (const anchor of anchors) {
      const href = anchor.href || '';
      if (!href.startsWith('http')) continue;
      if (href.includes('maps.google.') || href.includes('google.com/maps') || href.includes('google.com/finance')) continue;
      if (href.includes('google.com/sorry') || href.includes('google.com/policies')) continue;
      if (href.includes('/search?') || href.includes('accounts.google') || href.includes('support.google')) continue;
      const block = anchor.closest('div');
      const titleEl = anchor.querySelector('h3') || block?.querySelector('h3');
      const title = titleEl?.innerText?.trim() || anchor.innerText?.trim() || '';
      if (!title || title.length < 3) continue;
      if (seen.has(href)) continue;
      seen.add(href);

      const container = anchor.closest('div[data-sokoban-container], div.g, div.MjjYud') || anchor.parentElement;
      const snippet = Array.from(container?.querySelectorAll('span, div') || [])
        .map((el) => el.innerText?.trim() || '')
        .filter((text) => text && text !== title && text.length > 20)
        .slice(0, 4)
        .join(' ')
        .slice(0, 500);

      rows.push({ title, url: href, snippet, engine: 'google' });
      if (rows.length >= maxResults) break;
    }
    return rows;
  }, limit);
  return results;
}

function isGoogleBlockError(errorText) {
  return String(errorText || '').includes('Google automated-traffic interstitial');
}

async function handleGoogleConsent(page) {
  const candidates = [
    'button:has-text("Accept all")',
    'button:has-text("I agree")',
    'button:has-text("Reject all")',
    'button:has-text("Accept")',
    '[role="button"]:has-text("Accept all")',
    '[role="button"]:has-text("I agree")',
    '[role="button"]:has-text("Reject all")',
  ];
  for (const selector of candidates) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.isVisible({ timeout: 1500 })) {
        await locator.click({ delay: jitter(50, 150), timeout: 5000 });
        await page.waitForTimeout(jitter(800, 1400));
        return;
      }
    } catch {
      // Try next candidate.
    }
  }
  try {
    await page.keyboard.press('Escape');
    await page.waitForTimeout(jitter(300, 700));
  } catch {
    // Ignore.
  }
}

async function detectGoogleBlock(page) {
  const url = page.url();
  const body = (await page.locator('body').innerText({ timeout: 5000 }).catch(() => '')).toLowerCase();
  if (
    url.includes('/sorry/') ||
    body.includes('unusual traffic') ||
    body.includes('detected unusual traffic') ||
    body.includes('our systems have detected') ||
    body.includes('captcha') ||
    body.includes('not a robot')
  ) {
    throw new Error('Google automated-traffic interstitial');
  }
}

async function bingSearch(page, query, limit) {
  const url = `https://www.bing.com/search?q=${encodeURIComponent(query)}`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(1200);
  return await page.evaluate((maxResults) => {
    const rows = [];
    const items = Array.from(document.querySelectorAll('li.b_algo, .b_algo'));
    for (const item of items) {
      const anchor = item.querySelector('h2 a[href], a[href]');
      const title = anchor?.innerText?.trim() || '';
      const url = anchor?.href || '';
      const snippet = item.querySelector('p')?.innerText?.trim() || '';
      if (title && url) rows.push({ title, url, snippet, engine: 'bing' });
      if (rows.length >= maxResults) break;
    }
    return rows;
  }, limit);
}

function nameParts(value) {
  return String(value || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((part) => part.length >= 4 && !['van', 'der', 'den', 'the', 'and'].includes(part));
}

function isPromisingProfileResult(task, result) {
  const url = String(result.url || '').toLowerCase();
  if (!url.includes('linkedin.') || !url.includes('/in/')) return false;
  const haystack = `${result.title || ''} ${result.url || ''} ${result.snippet || ''}`.toLowerCase();
  const parts = nameParts(task.person_name || '');
  if (parts.length === 0) return false;
  const hits = parts.filter((part) => haystack.includes(part)).length;
  return hits === parts.length;
}

async function searchOne(page, task, limit) {
  const queries = Array.isArray(task.queries) && task.queries.length > 0 ? task.queries : [task.query || ''];
  const engines = [googleSearch, yahooSearch, bingSearch];
  const errors = [];
  let bestRows = [];
  let bestQuery = '';
  for (const query of queries) {
    const rows = [];
    const seenUrls = new Set();
    for (const engine of engines) {
      try {
        const results = await engine(page, query, limit);
        if (results.length > 0) {
          for (const result of results) {
            const url = result.url || '';
            if (!url || seenUrls.has(url)) continue;
            seenUrls.add(url);
            rows.push({ ...result, query });
          }
          if (rows.some((result) => isPromisingProfileResult(task, result))) {
            return { ...task, query_used: query, status: 'searched', results: rows };
          }
        }
        if (results.length === 0) errors.push(`${query} / ${engine.name}: no results`);
      } catch (error) {
        if (isGoogleBlockError(error.message)) {
          return { ...task, query_used: query, status: 'captcha', error: error.message, results: [] };
        }
        errors.push(`${query} / ${engine.name}: ${error.message}`);
      }
    }
    if (rows.length > 0 && bestRows.length === 0) {
      bestRows = rows;
      bestQuery = query;
    }
    if (rows.length === 0) errors.push(`${query}: no engine returned results`);
  }
  if (bestRows.length > 0) return { ...task, query_used: bestQuery, status: 'searched', results: bestRows };
  return { ...task, status: 'error', error: errors.join('; '), results: [] };
}

async function main() {
  const inputPath = argValue('--input');
  const outputPath = argValue('--output');
  const limit = Number(argValue('--limit', '5'));
  const delayMs = Number(argValue('--delay-ms', '1000'));
  const minDelayMs = Number(argValue('--min-delay-ms', String(delayMs)));
  const maxDelayMs = Number(argValue('--max-delay-ms', String(delayMs)));
  const headed = hasFlag('--headed');
  const chromePath = argValue('--chrome-path', process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  const userDataDir = argValue('--user-data-dir', process.env.PLAYWRIGHT_USER_DATA_DIR || '');
  const profileRoot = argValue('--profile-root', process.env.PLAYWRIGHT_PROFILE_ROOT || userDataDir || 'state/lead_exec_research/playwright_profiles');
  const profileCount = Number(argValue('--profile-count', '8'));
  const activeWorkerCount = Number(argValue('--active-workers', '4'));
  const robotCooldownMinMs = Number(argValue('--robot-cooldown-min-ms', argValue('--captcha-replacement-delay-ms', '20000')));
  const robotCooldownMaxMs = Number(argValue('--robot-cooldown-max-ms', '40000'));
  const robotRetryLimit = Number(argValue('--robot-retry-limit', '1'));
  if (!inputPath || !outputPath) {
    console.error('Usage: playwright_search_tasks.mjs --input tasks.json --output results.json [--limit 5] [--headed]');
    process.exit(2);
  }
  if (profileCount < 1) {
    console.error('--profile-count must be >= 1');
    process.exit(2);
  }
  if (activeWorkerCount < 1) {
    console.error('--active-workers must be >= 1');
    process.exit(2);
  }
  if (robotCooldownMinMs < 0 || robotCooldownMaxMs < robotCooldownMinMs) {
    console.error('--robot-cooldown-min-ms must be >= 0 and --robot-cooldown-max-ms must be >= min');
    process.exit(2);
  }
  if (robotRetryLimit < 0) {
    console.error('--robot-retry-limit must be >= 0');
    process.exit(2);
  }

  const input = JSON.parse(await fs.readFile(inputPath, 'utf8'));
  const tasks = Array.isArray(input) ? input : input.tasks || input.search_tasks || [];
  const activeSlots = Math.min(activeWorkerCount, profileCount, Math.max(tasks.length, 1));
  const contextOptions = {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    viewport: { width: 1400, height: 900 },
    locale: 'en-US',
    timezoneId: 'Africa/Lagos',
  };
  async function launchContext(profile) {
    await fs.mkdir(profile.dir, { recursive: true });
    const context = profile.dir
      ? await chromium.launchPersistentContext(profile.dir, {
          headless: !headed,
          executablePath: chromePath,
          args: ['--disable-blink-features=AutomationControlled'],
          ...contextOptions,
        })
      : await (async () => {
          const browser = await chromium.launch({
            headless: !headed,
            executablePath: chromePath,
            args: ['--disable-blink-features=AutomationControlled'],
          });
          const browserContext = await browser.newContext(contextOptions);
          browserContext.__browser = browser;
          return browserContext;
        })();
    const page = await context.newPage();
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    });
    return { context, page };
  }

  async function closeContext(context) {
    if (!context) return;
    const closePromise = context.__browser ? context.__browser.close() : context.close();
    await Promise.race([
      closePromise,
      sleep(10000).then(() => {
        output.worker_events.push({
          at: new Date().toISOString(),
          event: 'close_context_timeout',
        });
      }),
    ]);
  }

  const profiles = Array.from({ length: profileCount }, (_, index) => ({
    id: index + 1,
    dir: path.join(profileRoot, `profile-${index + 1}`),
    status: 'available',
    captcha_events: 0,
    completed_tasks: 0,
  }));
  const queues = Array.from({ length: activeSlots }, () => []);
  tasks.forEach((task, index) => {
    queues[index % activeSlots].push(task);
  });
  const output = {
    created_at: new Date().toISOString(),
    input_file: inputPath,
    profile_root: profileRoot,
    profile_count: profileCount,
    active_workers: activeSlots,
    min_delay_ms: minDelayMs,
    max_delay_ms: maxDelayMs,
    robot_cooldown_min_ms: robotCooldownMinMs,
    robot_cooldown_max_ms: robotCooldownMaxMs,
    robot_retry_limit: robotRetryLimit,
    task_count: tasks.length,
    results: [],
    captcha_events: 0,
    manual_fallback_tasks: [],
    hot_profiles: [],
    profile_transfers: [],
    pending_tasks: [],
    worker_events: [],
    status: 'running',
  };

  function takeAvailableProfile() {
    const profile = profiles.find((item) => item.status === 'available');
    if (!profile) return null;
    profile.status = 'active';
    return profile;
  }

  async function writeCheckpoint() {
    await fs.mkdir(path.dirname(outputPath), { recursive: true });
    output.completed_tasks = output.results.length;
    output.remaining_tasks = output.pending_tasks.length;
    output.profiles = profiles.map(({ id, dir, status, captcha_events, completed_tasks }) => ({
      id,
      dir,
      status,
      captcha_events,
      completed_tasks,
    }));
    await fs.writeFile(outputPath, JSON.stringify(output, null, 2) + '\n');
  }

  async function processQueue(slotId, profile, queue) {
    let context;
    let page;
    output.worker_events.push({
      at: new Date().toISOString(),
      event: 'worker_started',
      slot_id: slotId,
      profile_id: profile.id,
      queued_tasks: queue.length,
    });
    try {
      ({ context, page } = await launchContext(profile));
      while (queue.length > 0) {
        const task = queue.shift();
        const result = await searchOne(page, task, limit);
        if (result.status === 'captcha' || isGoogleBlockError(result.error)) {
          output.captcha_events += 1;
          profile.captcha_events += 1;
          profile.status = 'hot';
          output.hot_profiles.push({
            profile_id: profile.id,
            at: new Date().toISOString(),
            task: taskSummary(task),
          });
          await closeContext(context);
          context = null;
          const exhaustedRobotRetries = robotRetryCount(task) >= robotRetryLimit;
          if (exhaustedRobotRetries) {
            const fallbackResult = {
              ...task,
              query_used: result.query_used || task.query || '',
              status: 'manual_research_required',
              error: `Robot test repeated after ${robotRetryCount(task)} retry attempt(s).`,
              results: [],
            };
            output.results.push(fallbackResult);
            output.manual_fallback_tasks.push({
              at: new Date().toISOString(),
              reason: 'robot_test_retry_limit',
              task: taskSummary(task),
            });
            profile.completed_tasks += 1;
          }
          const replacementQueue = exhaustedRobotRetries ? [...queue] : [withRobotRetry(task), ...queue];
          const cooldownMs = jitter(robotCooldownMinMs, robotCooldownMaxMs);
          output.worker_events.push({
            at: new Date().toISOString(),
            event: exhaustedRobotRetries ? 'robot_test_manual_fallback' : 'robot_test_retry_scheduled',
            slot_id: slotId,
            profile_id: profile.id,
            cooldown_ms: cooldownMs,
            retry_count: robotRetryCount(task),
            reassigned_tasks: replacementQueue.length,
          });
          await writeCheckpoint();
          if (replacementQueue.length === 0) return;
          await sleep(cooldownMs);
          const replacement = takeAvailableProfile();
          if (!replacement) {
            output.pending_tasks.push(...replacementQueue);
            output.worker_events.push({
              at: new Date().toISOString(),
              event: 'paused_all_profiles_hot',
              slot_id: slotId,
              pending_tasks: replacementQueue.length,
            });
            await writeCheckpoint();
            return;
          }
          output.profile_transfers.push({
            at: new Date().toISOString(),
            from_profile_id: profile.id,
            to_profile_id: replacement.id,
            slot_id: slotId,
            reassigned_tasks: replacementQueue.length,
          });
          await processQueue(slotId, replacement, replacementQueue);
          return;
        }
        output.results.push(result);
        profile.completed_tasks += 1;
        await writeCheckpoint();
        if (queue.length > 0) {
          await sleep(jitter(minDelayMs, maxDelayMs));
        }
      }
      profile.status = 'finished';
      output.worker_events.push({
        at: new Date().toISOString(),
        event: 'worker_completed',
        slot_id: slotId,
        profile_id: profile.id,
      });
    } catch (error) {
      profile.status = profile.status === 'hot' ? 'hot' : 'error';
      output.pending_tasks.push(...queue);
      output.worker_events.push({
        at: new Date().toISOString(),
        event: 'worker_error',
        slot_id: slotId,
        profile_id: profile.id,
        error: error.message,
        pending_tasks: queue.length,
      });
      await writeCheckpoint();
    } finally {
      await closeContext(context);
    }
  }

  const workerPromises = queues
    .filter((queue) => queue.length > 0)
    .map((queue, index) => {
      const profile = takeAvailableProfile();
      if (!profile) {
        output.pending_tasks.push(...queue);
        return Promise.resolve();
      }
      return processQueue(index + 1, profile, queue);
    });
  await Promise.all(workerPromises);
  output.finished_at = new Date().toISOString();
  output.completed_tasks = output.results.length;
  output.remaining_tasks = output.pending_tasks.length;
  output.status = output.pending_tasks.length ? 'paused_due_to_all_profiles_hot' : 'completed';
  await writeCheckpoint();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
