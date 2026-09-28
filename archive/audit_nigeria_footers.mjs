#!/usr/bin/env node
/**
 * Read-only audit of lead-company websites for "Nigeria" in rendered footer text.
 *
 * The default audit target is rows 2–927 of the Hydro_incomplete tab in the
 * supplied "For Me" workbook. An optional upstream source can be used to show whether an audited
 * website or company name is also present in that original source. Results are
 * written locally under output/ and are never written back to Google Sheets.
 *
 * Examples:
 *   node scripts/audit_nigeria_footers.mjs --limit 100
 *   node scripts/audit_nigeria_footers.mjs --all --concurrency 4
 *   node scripts/audit_nigeria_footers.mjs --origin-sheet-url "https://docs.google.com/spreadsheets/d/.../edit"
 */

import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { chromium } from 'playwright';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const DEFAULT_LIMIT = 926;
const DEFAULT_CONCURRENCY = 4;
const DEFAULT_TIMEOUT_MS = 25_000;
const DEFAULT_AUDIT_SHEET_URL = 'https://docs.google.com/spreadsheets/d/1YF8WvLTPu-Raf22rHaaphauj-w-n64CRu83ZeAtHYAc/edit?gid=1031431672#gid=1031431672';
const DEFAULT_AUDIT_TAB = 'Hydro_incomplete';

function argValue(name, fallback = '') {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function readEnvFile(contents) {
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const [key, ...parts] = line.split('=');
    if (key.trim() && process.env[key.trim()] === undefined) {
      process.env[key.trim()] = parts.join('=').trim();
    }
  }
}

async function loadRepoEnv() {
  try {
    readEnvFile(await fs.readFile(path.join(ROOT, '.env'), 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
}

function base64Url(value) {
  return Buffer.from(value).toString('base64url');
}

async function googleAccessToken(credentials) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const claim = base64Url(JSON.stringify({
    iss: credentials.client_email,
    scope: 'https://www.googleapis.com/auth/spreadsheets.readonly',
    aud: credentials.token_uri || 'https://oauth2.googleapis.com/token',
    iat: now,
    exp: now + 3600,
  }));
  const unsignedJwt = `${header}.${claim}`;
  const signature = crypto.sign('RSA-SHA256', Buffer.from(unsignedJwt), credentials.private_key).toString('base64url');
  const response = await fetch(credentials.token_uri || 'https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: `${unsignedJwt}.${signature}`,
    }),
  });
  const body = await response.json();
  if (!response.ok || !body.access_token) {
    throw new Error(`Google authentication failed (${response.status}): ${body.error_description || body.error || 'unknown error'}`);
  }
  return body.access_token;
}

function spreadsheetIdFromUrl(value) {
  const match = String(value || '').match(/\/spreadsheets\/d\/([A-Za-z0-9_-]+)/);
  if (!match) throw new Error('Expected a Google Sheets URL containing /spreadsheets/d/<id>.');
  return match[1];
}

function spreadsheetGidFromUrl(value) {
  const match = String(value || '').match(/[?#&]gid=(\d+)/);
  return match ? Number(match[1]) : null;
}

function valueAt(row, index) {
  return index >= 0 && index < row.length ? String(row[index] || '').trim() : '';
}

function findHeaderIndex(headers, requested, aliases) {
  const normalized = new Map(headers.map((header, index) => [String(header).trim().toLowerCase(), index]));
  for (const candidate of [requested, ...aliases]) {
    const index = normalized.get(String(candidate).trim().toLowerCase());
    if (index !== undefined) return index;
  }
  return -1;
}

function normalizeWebsite(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const withScheme = raw.startsWith('//') ? `https:${raw}` : /^(https?):\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    const parsed = new URL(withScheme);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return '';
    return parsed.toString();
  } catch {
    return '';
  }
}

function normalizeName(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function compactText(value, maxLength = 320) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function nigeriaEvidence(footerText) {
  const match = /nigeria/i.exec(footerText);
  if (!match) return '';
  const start = Math.max(0, match.index - 120);
  const end = Math.min(footerText.length, match.index + match[0].length + 160);
  return compactText(footerText.slice(start, end));
}

async function closeWithin(closable, timeoutMs = 5_000) {
  await Promise.race([
    closable.close().catch(() => {}),
    new Promise((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
}

async function inspectWebsite(browser, website, timeoutMs) {
  const context = await browser.newContext({
    viewport: { width: 1366, height: 900 },
    locale: 'en-US',
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  });
  const page = await context.newPage();
  try {
    await page.goto(website, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await page.waitForTimeout(750);
    const result = await page.evaluate(() => {
      const selectors = ['footer', '[role="contentinfo"]', '[id*="footer" i]', '[class*="footer" i]'];
      const seen = new Set();
      const footerTexts = [];
      for (const selector of selectors) {
        for (const element of document.querySelectorAll(selector)) {
          if (seen.has(element)) continue;
          seen.add(element);
          const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
          if (text) footerTexts.push(text);
        }
      }
      return {
        page_title: document.title || '',
        footer_found: footerTexts.length > 0,
        footer_text: footerTexts.join('\n'),
      };
    });
    return {
      status: result.footer_found ? 'checked' : 'footer_not_found',
      final_url: page.url(),
      page_title: compactText(result.page_title, 180),
      footer_found: result.footer_found,
      nigeria_in_footer: /nigeria/i.test(result.footer_text),
      nigeria_evidence: nigeriaEvidence(result.footer_text),
      footer_text_length: result.footer_text.length,
    };
  } catch (error) {
    return {
      status: 'error',
      final_url: page.url() || '',
      page_title: '',
      footer_found: false,
      nigeria_in_footer: false,
      nigeria_evidence: '',
      footer_text_length: 0,
      error: compactText(error.message, 500),
    };
  } finally {
    await closeWithin(context);
  }
}

async function runWorkers(items, concurrency, worker) {
  let nextIndex = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (true) {
      const index = nextIndex++;
      if (index >= items.length) return;
      await worker(items[index], index);
    }
  });
  await Promise.all(workers);
}

async function sheetMetadata(spreadsheetId, token) {
  const response = await fetch(
    `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}?fields=sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))`,
    { headers: { authorization: `Bearer ${token}` } },
  );
  const payload = await response.json();
  if (!response.ok) throw new Error(`Unable to read spreadsheet metadata: ${payload.error?.message || response.statusText}`);
  return payload.sheets || [];
}

function resolveTabName(sheets, requestedName, url, label) {
  if (requestedName) return requestedName;
  const gid = spreadsheetGidFromUrl(url);
  const match = gid === null ? null : sheets.find((sheet) => sheet.properties?.sheetId === gid);
  if (match?.properties?.title) return match.properties.title;
  throw new Error(`${label} tab is required. Pass --${label.toLowerCase().replace(/ /g, '-')}-tab or include a valid gid in its URL.`);
}

async function sheetValues(spreadsheetId, tab, token) {
  const range = `${tab}!A:Z`;
  const response = await fetch(
    `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/${encodeURIComponent(range)}?majorDimension=ROWS`,
    { headers: { authorization: `Bearer ${token}` } },
  );
  const payload = await response.json();
  if (!response.ok) throw new Error(`Unable to read ${tab}: ${payload.error?.message || response.statusText}`);
  return payload.values || [];
}

function buildOriginIndex(values) {
  if (values.length < 2) return { rows: 0, websiteDomains: new Set(), companyNames: new Set() };
  const headers = values[0];
  const websiteIndex = findHeaderIndex(headers, 'Company Website', ['Website', 'website', 'URL']);
  const companyIndex = findHeaderIndex(headers, 'Company Name', ['Company', 'name', 'Name']);
  const websiteDomains = new Set();
  const companyNames = new Set();
  for (const row of values.slice(1)) {
    const website = normalizeWebsite(valueAt(row, websiteIndex));
    if (website) websiteDomains.add(new URL(website).hostname.replace(/^www\./, '').toLowerCase());
    const company = normalizeName(valueAt(row, companyIndex));
    if (company) companyNames.add(company);
  }
  return { rows: values.length - 1, websiteDomains, companyNames };
}

function originMatch(lead, originIndex) {
  if (!originIndex) return 'not_checked';
  const website = normalizeWebsite(lead.website);
  if (website && originIndex.websiteDomains.has(new URL(website).hostname.replace(/^www\./, '').toLowerCase())) return 'website';
  if (originIndex.companyNames.has(normalizeName(lead.company))) return 'company_name';
  return 'not_found';
}

function printUsage() {
  console.log(`Usage: node scripts/audit_nigeria_footers.mjs [options]

Read-only audit of source lead websites for the word "Nigeria" in rendered footer text.

Options:
  --all                         Check every source lead with a valid website.
  --limit <n>                   Maximum audit rows (default: ${DEFAULT_LIMIT}, i.e. sheet rows 2–927).
  --offset <n>                  Data-row offset for a later batch (default: 0, i.e. begins at sheet row 2).
  --sheet-url <url>             Audit workbook (default: Hydro_incomplete workbook).
  --source-tab <name>           Audit tab (default: ${DEFAULT_AUDIT_TAB}).
  --website-column <name>       Website header (default: website; Company Website also accepted).
  --origin-sheet-url <url>      Optional upstream source workbook for origin matching.
  --origin-tab <name>           Optional upstream source tab (uses the origin URL's gid if omitted).
  --concurrency <n>             Concurrent website checks (default: ${DEFAULT_CONCURRENCY}).
  --timeout-ms <n>              Per-site navigation timeout (default: ${DEFAULT_TIMEOUT_MS}).
  --output <path>               Local JSON report path.
  --headed                      Show browser windows while checking.
`);
}

async function main() {
  if (hasFlag('--help') || hasFlag('-h')) {
    printUsage();
    return;
  }
  await loadRepoEnv();
  const sourceUrl = argValue('--sheet-url', process.env.HYDRO_INCOMPLETE_SHEET_URL || DEFAULT_AUDIT_SHEET_URL);
  const sourceTab = argValue('--source-tab', process.env.HYDRO_INCOMPLETE_TAB || DEFAULT_AUDIT_TAB);
  const requestedWebsiteColumn = argValue('--website-column', 'website');
  const originUrl = argValue('--origin-sheet-url', '');
  const requestedOriginTab = argValue('--origin-tab', '');
  const all = hasFlag('--all');
  const limit = all ? Number.POSITIVE_INFINITY : Number(argValue('--limit', String(DEFAULT_LIMIT)));
  const offset = Number(argValue('--offset', '0'));
  const concurrency = Number(argValue('--concurrency', String(DEFAULT_CONCURRENCY)));
  const timeoutMs = Number(argValue('--timeout-ms', String(DEFAULT_TIMEOUT_MS)));
  const headed = hasFlag('--headed');
  if (!sourceUrl) throw new Error('No audit sheet configured. Pass --sheet-url.');
  if (!Number.isInteger(offset) || offset < 0 || !(limit > 0) || !Number.isInteger(concurrency) || concurrency < 1 || !(timeoutMs > 0)) {
    throw new Error('--offset must be >= 0; --limit, --concurrency, and --timeout-ms must be positive.');
  }

  const credentialsPath = process.env.GOOGLE_SHEETS_CREDENTIALS;
  if (!credentialsPath) throw new Error('GOOGLE_SHEETS_CREDENTIALS is not configured.');
  const credentials = JSON.parse(await fs.readFile(credentialsPath, 'utf8'));
  const token = await googleAccessToken(credentials);
  const spreadsheetId = spreadsheetIdFromUrl(sourceUrl);
  const targetSheets = await sheetMetadata(spreadsheetId, token);
  const resolvedSourceTab = resolveTabName(targetSheets, sourceTab, sourceUrl, 'Audit');
  const values = await sheetValues(spreadsheetId, resolvedSourceTab, token);
  if (values.length < 2) throw new Error(`${sourceTab} has no lead rows to audit.`);

  let originIndex = null;
  let originSource = null;
  if (originUrl) {
    const originSpreadsheetId = spreadsheetIdFromUrl(originUrl);
    const originSheets = await sheetMetadata(originSpreadsheetId, token);
    const originTab = resolveTabName(originSheets, requestedOriginTab, originUrl, 'Origin');
    originIndex = buildOriginIndex(await sheetValues(originSpreadsheetId, originTab, token));
    originSource = { spreadsheet_id: originSpreadsheetId, sheet_url: originUrl, tab: originTab, rows: originIndex.rows };
  }

  const headers = values[0];
  const websiteIndex = findHeaderIndex(headers, requestedWebsiteColumn, ['Company Website', 'Website']);
  if (websiteIndex < 0) throw new Error(`Could not find the website column. Available headers: ${headers.join(', ')}`);
  const idIndex = findHeaderIndex(headers, 'ID', ['Lead ID']);
  const companyIndex = findHeaderIndex(headers, 'Company Name', ['Company', 'Name']);
  const countryIndex = findHeaderIndex(headers, 'Country', []);
  const sourceRows = values.slice(1).map((row, index) => ({
    source_row: index + 2,
    lead_id: valueAt(row, idIndex),
    company: valueAt(row, companyIndex),
    source_country: valueAt(row, countryIndex),
    website: valueAt(row, websiteIndex),
  }));
  const selectedRows = sourceRows.slice(offset, Number.isFinite(limit) ? offset + limit : undefined);
  const uniqueWebsites = new Map();
  for (const row of selectedRows) {
    row.normalized_website = normalizeWebsite(row.website);
    if (row.normalized_website && !uniqueWebsites.has(row.normalized_website)) {
      uniqueWebsites.set(row.normalized_website, null);
    }
  }

  console.log(`Loaded ${sourceRows.length.toLocaleString()} source leads; auditing ${selectedRows.length.toLocaleString()} rows and ${uniqueWebsites.size.toLocaleString()} unique websites.`);
  const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const browser = await chromium.launch({
    headless: !headed,
    executablePath: chromePath,
    args: ['--disable-blink-features=AutomationControlled'],
  });
  try {
    const websites = [...uniqueWebsites.keys()];
    await runWorkers(websites, concurrency, async (website, index) => {
      const result = await inspectWebsite(browser, website, timeoutMs);
      uniqueWebsites.set(website, result);
      const label = result.nigeria_in_footer ? 'NIGERIA' : result.status;
      console.log(`[${index + 1}/${websites.length}] ${label}: ${website}`);
    });
  } finally {
    await closeWithin(browser, 10_000);
  }

  const results = selectedRows.map(({ normalized_website, ...lead }) => ({
    ...lead,
    normalized_website,
    origin_source_match: originMatch(lead, originIndex),
    ...(normalized_website ? uniqueWebsites.get(normalized_website) : {
      status: 'missing_or_invalid_website',
      final_url: '',
      page_title: '',
      footer_found: false,
      nigeria_in_footer: false,
      nigeria_evidence: '',
      footer_text_length: 0,
    }),
  }));
  const summary = {
    source_leads_total: sourceRows.length,
    leads_selected: selectedRows.length,
    unique_websites_checked: uniqueWebsites.size,
    nigeria_footer_matches: results.filter((result) => result.nigeria_in_footer).length,
    checked: results.filter((result) => result.status === 'checked').length,
    footer_not_found: results.filter((result) => result.status === 'footer_not_found').length,
    errors: results.filter((result) => result.status === 'error').length,
    missing_or_invalid_website: results.filter((result) => result.status === 'missing_or_invalid_website').length,
    origin_website_matches: results.filter((result) => result.origin_source_match === 'website').length,
    origin_company_name_matches: results.filter((result) => result.origin_source_match === 'company_name').length,
  };
  const report = {
    created_at: new Date().toISOString(),
    purpose: 'Read-only check for the word "Nigeria" in rendered company-site footer text.',
    source: { spreadsheet_id: spreadsheetId, sheet_url: sourceUrl, tab: resolvedSourceTab, website_column: headers[websiteIndex] },
    origin_source: originSource,
    selection: { all, offset, limit: Number.isFinite(limit) ? limit : null },
    summary,
    results,
  };
  const defaultName = `nigeria_footer_audit_${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
  const outputPath = path.resolve(ROOT, argValue('--output', path.join('output', defaultName)));
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  console.log(`Report written to ${outputPath}`);
  console.log(`Summary: ${summary.nigeria_footer_matches} Nigeria-footer matches; ${summary.checked} checked; ${summary.errors} errors.`);
}

main().catch((error) => {
  console.error(`Nigeria footer audit failed: ${error.message}`);
  process.exitCode = 1;
});
