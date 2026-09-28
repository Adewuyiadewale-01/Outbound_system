#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import ExcelJS from "exceljs";
import { chromium } from "playwright";

const ROOT = path.resolve(new URL("..", import.meta.url).pathname);
const DEFAULT_TEMPLATE = path.join(ROOT, "prompts", "research-template.txt");
const DEFAULT_ANGLE_TEMPLATE = path.join(ROOT, "prompts", "message-angle-template.txt");
const DEFAULT_OUTPUT_DIR = path.join(ROOT, "output", "research");
const DEFAULT_PROFILE_DIR = path.join(ROOT, ".chatgpt-chrome-profile");

const FIELD_ALIASES = {
  prospectId: ["Prospect ID", "prospect_id", "ProspectID", "id"],
  company: ["Company", "company"],
  companyWebsite: ["Company Website", "company_website", "Website", "website"],
  contactName: ["Contact Name", "Person Engaged", "contact_name", "person_engaged"],
  contactLinkedin: ["Contact Linkedin", "Contact LinkedIn", "contact_linkedin", "LinkedIn", "linkedin"],
  approval: ["Approval", "approval", "Approved", "approved"],
  chatgptUrl: ["ChatGPT Conversation URL", "chatgpt_url", "ChatGPT URL"],
  researchSummary: ["Research summary", "research_summary", "Research Summary"],
  messageDraft: ["Message draft", "message_draft", "Message Draft"],
};

function usage() {
  console.log(`
Usage:
  npm run research -- --leads examples/leads.csv --row 1
  npm run research -- --sheet-url <google-sheet-url> --sheet Messaging --cdp-url http://127.0.0.1:9222

Options:
  --leads <path>          CSV, TSV, JSON, or XLSX file containing lead rows.
  --sheet-url <url>       Google Sheet URL. Fetches approved rows from this live sheet.
  --credentials <path>    Google Sheets service-account JSON. Defaults to GOOGLE_SHEETS_CREDENTIALS when set.
  --sheet <name>          Sheet name for XLSX input. Defaults to Messaging.
  --row <number>          1-based row number after the header. Defaults to 1.
  --prospect-id <id>      Select row by Prospect ID instead of row number.
  --approved-only         Process every row whose Approval column is checked/truthy.
  --include-completed     Include approved rows that already have a ChatGPT Conversation URL.
  --limit <number>        Maximum number of leads to process.
  --recapture-current     Save the latest substantial response from the current ChatGPT tab for the selected lead.
  --template <path>       Prompt template path. Defaults to prompts/research-template.txt.
  --angle-template <path> Follow-up template path. Defaults to prompts/message-angle-template.txt.
  --output-dir <path>     Output directory. Defaults to output/research.
  --profile-dir <path>    Chrome profile dir for ChatGPT login. Defaults to .chatgpt-chrome-profile.
  --chrome-profile-name <name>
                         Optional Chrome profile name, e.g. "Default" or "Profile 1".
  --cdp-url <url>         Attach to an already-open Chrome, e.g. http://127.0.0.1:9222.
  --headless              Run browser headless. Headed is the default.
  --login-only            Open ChatGPT with this profile and wait for manual login/setup.
  --with-angle            Send the follow-up message-angle prompt after research is captured.
  --angle-only            Send only the follow-up prompt in the current ChatGPT chat.
  --tier-waits-ms <list>   Comma-separated minimum wait tiers. Defaults to 60000.
  --max-wait-ms <number>  Maximum wait for finished response. Defaults to 250000.
  --stable-ms <number>    Required no-new-turn quiet time after finish controls. Defaults to 30000.
  --send-retries <number> Fresh-chat send attempts per lead. Defaults to 3.
  --assistant-start-timeout-ms <number>
                         Time allowed for ChatGPT to begin an assistant turn after submit. Defaults to 30000.
  --doc-webhook-url <url>
                         Optional Apps Script endpoint that creates a formatted Google Doc tab.
  --doc-webhook-secret <secret>
                         Secret for the Apps Script endpoint. Can also use DOC_WEBHOOK_SECRET.
  --google-doc-id <id>    Parent Google Doc ID for the created research tabs.
  --google-doc-url <url>  Parent Google Doc URL for the created research tabs.
  --dry-run               Build and save prompt without opening Chrome.
`);
}

function parseArgs(argv) {
  const args = {
    row: 1,
    sheet: "Messaging",
    credentials: process.env.GOOGLE_SHEETS_CREDENTIALS || "~/.openclaw/credentials/google-sheets.json",
    template: DEFAULT_TEMPLATE,
    angleTemplate: DEFAULT_ANGLE_TEMPLATE,
    outputDir: DEFAULT_OUTPUT_DIR,
    profileDir: process.env.CHATGPT_PROFILE_DIR || DEFAULT_PROFILE_DIR,
    headless: false,
    tierWaitsMs: [60000],
    maxWaitMs: 250000,
    stableMs: 30000,
    sendRetries: 3,
    assistantStartTimeoutMs: 30000,
    docWebhookUrl: process.env.DOC_WEBHOOK_URL || "",
    docWebhookSecret: process.env.DOC_WEBHOOK_SECRET || "",
    googleDocId: process.env.GOOGLE_DOC_ID || process.env.DOC_WEBHOOK_DOCUMENT_ID || "",
    googleDocUrl: process.env.GOOGLE_DOC_URL || "",
    dryRun: false,
    loginOnly: false,
    withAngle: false,
    angleOnly: false,
    approvedOnly: false,
    includeCompleted: false,
    limit: null,
    recaptureCurrent: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(0);
    }
    if (arg === "--headless") {
      args.headless = true;
      continue;
    }
    if (arg === "--dry-run") {
      args.dryRun = true;
      continue;
    }
    if (arg === "--login-only") {
      args.loginOnly = true;
      continue;
    }
    if (arg === "--with-angle") {
      args.withAngle = true;
      continue;
    }
    if (arg === "--angle-only") {
      args.angleOnly = true;
      continue;
    }
    if (arg === "--approved-only") {
      args.approvedOnly = true;
      continue;
    }
    if (arg === "--include-completed") {
      args.includeCompleted = true;
      continue;
    }
    if (arg === "--recapture-current") {
      args.recaptureCurrent = true;
      continue;
    }

    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      throw new Error(`Missing value for ${arg}`);
    }
    i += 1;

    if (arg === "--leads") args.leads = next;
    else if (arg === "--sheet-url") args.sheetUrl = next;
    else if (arg === "--credentials") args.credentials = expandHome(next);
    else if (arg === "--sheet") args.sheet = next;
    else if (arg === "--row") args.row = Number(next);
    else if (arg === "--prospect-id") args.prospectId = next;
    else if (arg === "--template") args.template = next;
    else if (arg === "--angle-template") args.angleTemplate = next;
    else if (arg === "--output-dir") args.outputDir = next;
    else if (arg === "--profile-dir") args.profileDir = expandHome(next);
    else if (arg === "--chrome-profile-name") args.chromeProfileName = next;
    else if (arg === "--cdp-url") args.cdpUrl = next;
    else if (arg === "--tier-waits-ms") args.tierWaitsMs = parseTierWaits(next);
    else if (arg === "--max-wait-ms") args.maxWaitMs = Number(next);
    else if (arg === "--stable-ms") args.stableMs = Number(next);
    else if (arg === "--send-retries") args.sendRetries = Number(next);
    else if (arg === "--assistant-start-timeout-ms") args.assistantStartTimeoutMs = Number(next);
    else if (arg === "--doc-webhook-url") args.docWebhookUrl = next;
    else if (arg === "--doc-webhook-secret") args.docWebhookSecret = next;
    else if (arg === "--google-doc-id") args.googleDocId = next;
    else if (arg === "--google-doc-url") args.googleDocUrl = next;
    else if (arg === "--limit") args.limit = Number(next);
    else throw new Error(`Unknown option: ${arg}`);
  }

  if (!args.leads && !args.sheetUrl && !args.loginOnly) throw new Error("--leads or --sheet-url is required");
  if (!Number.isFinite(args.row) || args.row < 1) throw new Error("--row must be a positive number");
  if (args.limit != null && (!Number.isFinite(args.limit) || args.limit < 1)) throw new Error("--limit must be a positive number");
  if (!Array.isArray(args.tierWaitsMs) || args.tierWaitsMs.length === 0) throw new Error("--tier-waits-ms is required");
  if (!Number.isFinite(args.maxWaitMs) || args.maxWaitMs < 10000) throw new Error("--max-wait-ms is too small");
  if (!Number.isFinite(args.stableMs) || args.stableMs < 1000) throw new Error("--stable-ms is too small");
  if (!Number.isFinite(args.sendRetries) || args.sendRetries < 1) throw new Error("--send-retries must be at least 1");
  if (!Number.isFinite(args.assistantStartTimeoutMs) || args.assistantStartTimeoutMs < 5000) {
    throw new Error("--assistant-start-timeout-ms is too small");
  }
  if (args.docWebhookUrl && !args.docWebhookSecret) throw new Error("--doc-webhook-secret is required when --doc-webhook-url is set");
  return args;
}

function parseTierWaits(value) {
  const waits = value.split(",").map((item) => Number(item.trim())).filter((item) => Number.isFinite(item));
  if (waits.some((item) => item < 1000)) throw new Error("--tier-waits-ms values must be at least 1000");
  return waits;
}

function expandHome(value) {
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

async function readLeadRows(filePath, sheetName = "Messaging") {
  const absolute = path.resolve(expandHome(filePath));
  const ext = path.extname(absolute).toLowerCase();

  if (ext === ".xls") {
    throw new Error("Legacy .xls input is not supported. Convert the file to .xlsx, CSV, TSV, or JSON first.");
  }

  if (ext === ".xlsx") {
    const workbook = new ExcelJS.Workbook();
    await workbook.xlsx.load(fs.readFileSync(absolute));
    const sheet = workbook.getWorksheet(sheetName) || workbook.worksheets[0];
    if (!sheet) return [];

    const headers = sheet.getRow(1).values.slice(1).map((value) => String(value || "").trim());
    const rows = [];
    sheet.eachRow({ includeEmpty: false }, (row, rowNumber) => {
      if (rowNumber === 1) return;
      const entry = {};
      headers.forEach((header, index) => {
        if (header) entry[header] = row.getCell(index + 1).text || "";
      });
      rows.push(normalizeRow(entry));
    });
    return rows;
  }

  const text = fs.readFileSync(absolute, "utf8");
  if (ext === ".json") {
    const data = JSON.parse(text);
    if (!Array.isArray(data)) throw new Error("JSON leads file must contain an array of row objects");
    return data.map((row) => normalizeRow(row));
  }

  const delimiter = ext === ".tsv" ? "\t" : ",";
  return parseDelimited(text, delimiter);
}

function fetchApprovedRowsFromGoogleSheet(args) {
  const scriptPath = path.join(ROOT, "scripts", "google-sheet-messaging.py");
  const commandArgs = [
    scriptPath,
    "--credentials",
    args.credentials,
    "--sheet-url",
    args.sheetUrl,
    "--tab",
    args.sheet,
    "fetch-approved",
  ];
  if (args.includeCompleted || args.prospectId) commandArgs.push("--include-completed");

  const result = spawnSync("python3", commandArgs, { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`Failed to fetch approved rows from Google Sheet:\n${result.stderr || result.stdout}`);
  }

  const payload = JSON.parse(result.stdout);
  if (!payload.ok) throw new Error(`Google Sheet fetch failed: ${result.stdout}`);
  return payload.rows.map((row) => normalizeRow(row));
}

function updateGoogleSheetResult(args, leadContext, chatgptUrl, docTab = null) {
  if (!args.sheetUrl) return;
  if (!leadContext.rowNumber) throw new Error(`Cannot update Google Sheet: missing row number for ${leadContext.prospectId}`);

  const scriptPath = path.join(ROOT, "scripts", "google-sheet-messaging.py");
  const commandArgs = [
    scriptPath,
    "--credentials",
    args.credentials,
    "--sheet-url",
    args.sheetUrl,
    "--tab",
    args.sheet,
    "update-result",
    "--row-number",
    String(leadContext.rowNumber),
    "--chatgpt-url",
    chatgptUrl,
    "--research-summary-file",
    leadContext.researchResponsePath,
  ];
  const googleDocId = docTab?.documentId || docTab?.docId || extractGoogleDocId(docTab?.documentUrl || docTab?.docUrl || "") || args.googleDocId || extractGoogleDocId(args.googleDocUrl);
  const googleDocUrl = docTab?.documentUrl || docTab?.docUrl || args.googleDocUrl || (googleDocId ? `https://docs.google.com/document/d/${googleDocId}/edit` : "");
  const googleDocTabId = docTab?.tabId || "";
  const googleDocTabTitle = docTab?.tabTitle || "";
  if (googleDocId) commandArgs.push("--google-doc-id", googleDocId);
  if (googleDocUrl) commandArgs.push("--google-doc-url", googleDocUrl);
  if (googleDocTabId) commandArgs.push("--google-doc-tab-id", googleDocTabId);
  if (googleDocTabTitle) commandArgs.push("--google-doc-tab-title", googleDocTabTitle);

  const result = spawnSync("python3", commandArgs, { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`Failed to update Google Sheet row ${leadContext.rowNumber}:\n${result.stderr || result.stdout}`);
  }
}

function extractGoogleDocId(url) {
  const match = String(url || "").match(/\/document\/d\/([^/]+)/);
  return match ? match[1] : "";
}

function normalizeRow(row) {
  const normalized = {};
  for (const [key, value] of Object.entries(row)) {
    normalized[String(key).trim()] = value == null ? "" : String(value).trim();
  }
  return normalized;
}

function parseDelimited(text, delimiter) {
  const rows = [];
  let cell = "";
  let row = [];
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];

    if (char === '"' && inQuotes && next === '"') {
      cell += '"';
      i += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === delimiter && !inQuotes) {
      row.push(cell);
      cell = "";
    } else if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && next === "\n") i += 1;
      row.push(cell);
      if (row.some((value) => value.trim() !== "")) rows.push(row);
      cell = "";
      row = [];
    } else {
      cell += char;
    }
  }

  row.push(cell);
  if (row.some((value) => value.trim() !== "")) rows.push(row);
  if (rows.length < 2) return [];

  const headers = rows[0].map((value) => value.trim());
  return rows.slice(1).map((values) => {
    const object = {};
    headers.forEach((header, index) => {
      object[header] = (values[index] || "").trim();
    });
    return object;
  });
}

function isApproved(row) {
  const value = getField(row, "approval").toLowerCase();
  return ["true", "yes", "y", "1", "checked", "approved", "x"].includes(value);
}

function isCompleted(row) {
  return Boolean(getField(row, "chatgptUrl"));
}

function selectLeads(rows, args) {
  if (args.approvedOnly) {
    return rows.filter((row) => isApproved(row) && (args.includeCompleted || !isCompleted(row)));
  }
  return [selectLead(rows, args)];
}

function getField(row, canonicalName) {
  const aliases = FIELD_ALIASES[canonicalName] || [canonicalName];
  for (const alias of aliases) {
    if (row[alias] != null && String(row[alias]).trim() !== "") {
      return String(row[alias]).trim();
    }
  }
  return "";
}

function selectLead(rows, args) {
  if (args.prospectId) {
    const match = rows.find((row) => getField(row, "prospectId") === args.prospectId);
    if (!match) throw new Error(`No row found for Prospect ID: ${args.prospectId}`);
    return match;
  }

  const lead = rows[args.row - 1];
  if (!lead) throw new Error(`No row found at row ${args.row}`);
  return lead;
}

function buildPrompt(lead, templatePath) {
  const template = fs.readFileSync(path.resolve(expandHome(templatePath)), "utf8");
  const companyWebsite = getField(lead, "companyWebsite");
  const contactName = getField(lead, "contactName");
  const contactLinkedin = getField(lead, "contactLinkedin");

  if (!companyWebsite) throw new Error("Lead is missing Company Website");
  if (!contactName) throw new Error("Lead is missing Contact Name / Person Engaged");

  const contactLinkedinLine = contactLinkedin
    ? `Here is the contact person's LinkedIn URL if it is useful for the research: ${contactLinkedin}\n\n`
    : "";

  return template
    .replaceAll("{{companyWebsite}}", companyWebsite)
    .replaceAll("{{contactName}}", contactName)
    .replaceAll("{{contactLinkedin}}", contactLinkedin)
    .replaceAll("{{contactLinkedinLine}}", contactLinkedinLine);
}

function buildAnglePrompt(templatePath) {
  return fs.readFileSync(path.resolve(expandHome(templatePath)), "utf8");
}

async function openBrowser(args) {
  if (args.cdpUrl) {
    const browser = await chromium.connectOverCDP(args.cdpUrl);
    const context = browser.contexts()[0] || (await browser.newContext());
    return {
      context,
      close: async () => {},
    };
  }

  const context = await chromium.launchPersistentContext(path.resolve(args.profileDir), {
    channel: "chrome",
    headless: args.headless,
    viewport: { width: 1440, height: 1000 },
    permissions: ["clipboard-read", "clipboard-write"],
    args: args.chromeProfileName ? [`--profile-directory=${args.chromeProfileName}`] : [],
  });

  return {
    context,
    close: async () => {
      await context.close();
    },
  };
}

function slugify(value) {
  return String(value || "lead")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 80) || "lead";
}

function writeText(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, value, "utf8");
}

function readClipboardText() {
  if (process.platform === "darwin") {
    const result = spawnSync("pbpaste", { encoding: "utf8" });
    if (result.status === 0) return result.stdout;
  }
  return "";
}

function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
  return text;
}

function appendOutputIndex(outputRoot, row) {
  const filePath = path.resolve(outputRoot, "messaging-research-report.csv");
  const headers = [
    "Prospect ID",
    "Company",
    "Company Website",
    "Contact Name",
    "Contact Linkedin",
    "Approval",
    "ChatGPT Conversation URL",
    "Research summary",
    "Google Doc ID",
    "Google Doc URL",
    "Google Doc Tab ID",
    "Google Doc Tab Title",
    "Message draft",
  ];

  const exists = fs.existsSync(filePath);
  const values = headers.map((header) => csvEscape(row[header] || ""));
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, `${exists ? "" : `${headers.join(",")}\n`}${values.join(",")}\n`, "utf8");
}

async function pasteIntoPrompt(page, prompt) {
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)).catch(() => {});
  const composer = page.locator(
    '#prompt-textarea[contenteditable="true"][aria-label="Chat with ChatGPT"], [data-testid="prompt-textarea"][contenteditable="true"][aria-label="Chat with ChatGPT"], textarea[aria-label="Chat with ChatGPT"], textarea[name="prompt-textarea"]'
  );
  await composer.first().waitFor({ state: "attached", timeout: 60000 });

  const wroteClipboard = await page.evaluate(async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }, prompt);

  const visibleEditable = page
    .locator('#prompt-textarea[contenteditable="true"][aria-label="Chat with ChatGPT"], [data-testid="prompt-textarea"][contenteditable="true"][aria-label="Chat with ChatGPT"]')
    .first();

  if ((await visibleEditable.isVisible().catch(() => false)) && wroteClipboard) {
    await visibleEditable.scrollIntoViewIfNeeded().catch(() => {});
    await visibleEditable.click({ force: true });
    await assertComposerFocused(page);
    await page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
    return;
  }

  if (await visibleEditable.isVisible().catch(() => false)) {
    await visibleEditable.scrollIntoViewIfNeeded().catch(() => {});
    await visibleEditable.click({ force: true });
    await assertComposerFocused(page);
    await page.keyboard.insertText(prompt);
    return;
  }

  const wroteDirectly = await page.evaluate((text) => {
    const textarea = document.querySelector('textarea[aria-label="Chat with ChatGPT"], textarea[name="prompt-textarea"]');
    if (!textarea) return false;
    textarea.value = text;
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.focus();
    return true;
  }, prompt);

  if (!wroteDirectly) {
    throw new Error("Could not find a usable ChatGPT prompt composer");
  }

  if (wroteClipboard) {
    await assertComposerFocused(page);
    await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    await page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
  } else {
    await assertComposerFocused(page);
    await page.keyboard.insertText(prompt.slice(0, 1000));
  }
}

async function assertComposerFocused(page) {
  const focusedIsComposer = await page.evaluate(() => {
    const active = document.activeElement;
    return Boolean(
      active &&
        (active.matches('#prompt-textarea[aria-label="Chat with ChatGPT"]') ||
          active.matches('textarea[aria-label="Chat with ChatGPT"], textarea[name="prompt-textarea"]'))
    );
  });

  if (!focusedIsComposer) {
    throw new Error("Refusing to type: ChatGPT composer is not focused");
  }
}

async function submitPrompt(page) {
  const sendButton = page.locator('[data-testid="send-button"], button[aria-label="Send prompt"]').last();

  if (await sendButton.isVisible().catch(() => false)) {
    await sendButton.click({ timeout: 10000 });
  } else {
    await page.keyboard.press("Enter");
  }
}

async function openFreshChat(page) {
  const ensureChatGptShell = async (url) => {
    const currentUrl = page.url();
    if (currentUrl.startsWith("https://chatgpt.com/")) return;
    await page.goto(url, { waitUntil: "commit", timeout: 60000 });
    await page
      .locator(
        '#prompt-textarea[contenteditable="true"][aria-label="Chat with ChatGPT"], [data-testid="prompt-textarea"][contenteditable="true"][aria-label="Chat with ChatGPT"], textarea[aria-label="Chat with ChatGPT"], textarea[name="prompt-textarea"]'
      )
      .first()
      .waitFor({ state: "attached", timeout: 60000 });
  };

  await ensureChatGptShell("https://chatgpt.com/");
  const newChat = page.locator('[data-testid="create-new-chat-button"]').first();
  if (await newChat.isVisible().catch(() => false)) {
    await newChat.click({ timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(1000);
  }

  const userCount = await page.locator('[data-message-author-role="user"]').count().catch(() => 0);
  const assistantCount = await page.locator('[data-message-author-role="assistant"]').count().catch(() => 0);
  if (userCount || assistantCount) {
    await page.goto("https://chatgpt.com/?_new=" + Date.now(), { waitUntil: "commit", timeout: 60000 });
    await page
      .locator(
        '#prompt-textarea[contenteditable="true"][aria-label="Chat with ChatGPT"], [data-testid="prompt-textarea"][contenteditable="true"][aria-label="Chat with ChatGPT"], textarea[aria-label="Chat with ChatGPT"], textarea[name="prompt-textarea"]'
      )
      .first()
      .waitFor({ state: "attached", timeout: 60000 });
    await page.waitForTimeout(1000);
  }
}

async function sendPrompt(page, prompt) {
  const assistantCountBefore = await page.locator('[data-message-author-role="assistant"]').count().catch(() => 0);
  const responseActionCountBefore = await responseActionButtons(page).count().catch(() => 0);
  const userCountBefore = await page.locator('[data-message-author-role="user"]').count().catch(() => 0);
  await pasteIntoPrompt(page, prompt);
  await submitPrompt(page);
  await waitForUserMessageSubmitted(page, userCountBefore);
  return { assistantCountBefore, responseActionCountBefore };
}

async function waitForAssistantTurnStarted(page, assistantCountBefore, timeoutMs) {
  const assistantMessages = page.locator('[data-message-author-role="assistant"]');
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    const count = await assistantMessages.count().catch(() => 0);
    if (count > assistantCountBefore) {
      console.log(`Assistant turn started after ${Math.round((Date.now() - startedAt) / 1000)}s`);
      return;
    }

    const stopVisible = await page
      .locator('[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="stop"]')
      .first()
      .isVisible()
      .catch(() => false);
    if (stopVisible) return;

    await page.waitForTimeout(2000);
  }

  throw new Error(`ChatGPT accepted the user message but no assistant response started within ${timeoutMs}ms`);
}

async function waitForUserMessageSubmitted(page, userCountBefore) {
  const start = Date.now();
  const timeoutMs = 30000;
  const userMessages = page.locator('[data-message-author-role="user"]');

  while (Date.now() - start < timeoutMs) {
    const userCount = await userMessages.count().catch(() => 0);
    if (userCount > userCountBefore) return;

    await page.waitForTimeout(1500);
  }

  throw new Error("Prompt was not submitted: no new user message appeared in ChatGPT");
}

async function waitForResearchResponse(page, timeoutMs, stableMs, assistantCountBefore = 0) {
  const assistantMessages = page.locator('[data-message-author-role="assistant"]');
  const start = Date.now();
  let lastText = "";
  let lastChange = Date.now();

  while (Date.now() - start < timeoutMs) {
    const count = await assistantMessages.count().catch(() => 0);
    const text = count > assistantCountBefore ? (await assistantMessages.nth(count - 1).innerText().catch(() => "")).trim() : "";

    if (text && text !== lastText) {
      lastText = text;
      lastChange = Date.now();
    }

    const stopVisible = await page
      .locator('[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="stop"]')
      .first()
      .isVisible()
      .catch(() => false);

    if (lastText && !stopVisible && Date.now() - lastChange >= stableMs) {
      return lastText;
    }

    await page.waitForTimeout(3000);
  }

  if (lastText) return lastText;
  throw new Error(`Timed out after ${timeoutMs}ms without capturing an assistant response`);
}

function responseActionButtons(page) {
  return page.locator(
    [
      '[data-testid="copy-turn-action-button"]',
      '[data-testid="good-response-turn-action-button"]',
      '[data-testid="bad-response-turn-action-button"]',
      'button[aria-label="Copy response"]',
      'button[aria-label="Good response"]',
      'button[aria-label="Bad response"]',
    ].join(", ")
  );
}

async function hasFinishedResponseSignal(page, assistantCountBefore = 0, responseActionCountBefore = 0) {
  return await page.evaluate(
    ({ assistantCountBefore, responseActionCountBefore }) => {
      const assistantNodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
      if (assistantNodes.length <= assistantCountBefore) return false;

      const stopButton = [...document.querySelectorAll("button")].find((button) => {
        const label = `${button.getAttribute("data-testid") || ""} ${button.getAttribute("aria-label") || ""} ${button.innerText || ""}`;
        const rect = button.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && /stop/i.test(label);
      });
      if (stopButton) return false;

      const latestAssistant = assistantNodes[assistantNodes.length - 1];
      const latestText = latestAssistant.innerText || "";
      if (latestText.trim().length < 1500) return false;

      const assistantRect = latestAssistant.getBoundingClientRect();
      const actionButtons = [...document.querySelectorAll("button")].filter((button) => {
        const testid = button.getAttribute("data-testid") || "";
        const aria = button.getAttribute("aria-label") || "";
        const rect = button.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return false;
        const isResponseAction =
          testid === "copy-turn-action-button" ||
          testid === "good-response-turn-action-button" ||
          testid === "bad-response-turn-action-button" ||
          aria === "Copy response" ||
          aria === "Good response" ||
          aria === "Bad response";
        if (!isResponseAction) return false;
        return (
          rect.y >= assistantRect.bottom - 40 &&
          rect.y <= assistantRect.bottom + 180 &&
          rect.x >= assistantRect.left - 80 &&
          rect.x <= assistantRect.right + 80
        );
      });

      const latestActionTypes = new Set(
        actionButtons.map((button) => button.getAttribute("data-testid") || button.getAttribute("aria-label") || "")
      );

      const allResponseActions = [...document.querySelectorAll("button")].filter((button) => {
        const testid = button.getAttribute("data-testid") || "";
        const aria = button.getAttribute("aria-label") || "";
        const rect = button.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return false;
        return (
          testid === "copy-turn-action-button" ||
          testid === "good-response-turn-action-button" ||
          testid === "bad-response-turn-action-button" ||
          aria === "Copy response" ||
          aria === "Good response" ||
          aria === "Bad response"
        );
      });

      return (
        latestActionTypes.has("copy-turn-action-button") &&
        latestActionTypes.has("good-response-turn-action-button") &&
        latestActionTypes.has("bad-response-turn-action-button") &&
        allResponseActions.length > responseActionCountBefore
      );
    },
    { assistantCountBefore, responseActionCountBefore }
  );
}

async function latestAssistantState(page) {
  return await page.evaluate(() => {
    const assistantNodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
    const latest = assistantNodes[assistantNodes.length - 1];
    return {
      count: assistantNodes.length,
      textLength: latest ? (latest.innerText || "").length : 0,
    };
  });
}

function isSubstantialResearchResponse(text) {
  const value = String(text || "").trim();
  if (value.length < 1500) return false;
  const lowered = value.toLowerCase();
  return (
    lowered.includes("company") ||
    lowered.includes("research") ||
    lowered.includes("source") ||
    lowered.includes("rudi") ||
    lowered.includes("jozua") ||
    lowered.includes("linkedin") ||
    lowered.includes("website")
  );
}

async function getLatestSubstantialAssistantMessage(page, assistantCountBefore = 0) {
  const assistantMessages = page.locator('[data-message-author-role="assistant"]');
  const count = await assistantMessages.count().catch(() => 0);
  for (let index = count - 1; index >= assistantCountBefore; index -= 1) {
    const text = (await assistantMessages.nth(index).innerText().catch(() => "")).trim();
    if (isSubstantialResearchResponse(text)) return text;
  }
  return "";
}

async function copyLatestAssistantResponse(page) {
  const result = await page.evaluate(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };

    const assistantNodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
    const latestAssistant = assistantNodes[assistantNodes.length - 1];
    if (!latestAssistant) return { ok: false, reason: "No assistant response found" };

    latestAssistant.scrollIntoView({ block: "end" });
    const assistantRect = latestAssistant.getBoundingClientRect();
    const copyButtons = [...document.querySelectorAll("button")].filter((button) => {
      const rect = button.getBoundingClientRect();
      return (
        visible(button) &&
        button.getAttribute("data-testid") === "copy-turn-action-button" &&
        button.getAttribute("aria-label") === "Copy response" &&
        rect.y >= assistantRect.bottom - 120 &&
        rect.y <= assistantRect.bottom + 260 &&
        rect.x >= assistantRect.left - 120 &&
        rect.x <= assistantRect.right + 120
      );
    });

    const copyButton = copyButtons[copyButtons.length - 1];
    if (!copyButton) {
      return {
        ok: false,
        reason: "Latest Copy response button not found",
        assistantCount: assistantNodes.length,
        latestTextLength: (latestAssistant.innerText || "").length,
      };
    }

    copyButton.click();
    return {
      ok: true,
      assistantCount: assistantNodes.length,
      latestTextLength: (latestAssistant.innerText || "").length,
    };
  });

  if (!result.ok) {
    throw new Error(`Could not copy latest ChatGPT response: ${result.reason}`);
  }

  await page.waitForTimeout(1500);
  const copied = readClipboardText().trim();
  if (!isSubstantialResearchResponse(copied)) {
    throw new Error(`Copied ChatGPT response did not look substantial enough (${copied.length} chars)`);
  }
  console.log(`Copied latest ChatGPT response from toolbar (${copied.length} chars)`);
  return copied;
}

function buildDocTabTitle(leadContext) {
  const compactCompany = String(leadContext.company || "Company")
    .replace(/\s+/g, " ")
    .replace(/[\\r\\n]/g, " ")
    .trim();
  const compactContact = String(leadContext.contactName || "Contact")
    .replace(/\s+/g, " ")
    .replace(/[\\r\\n]/g, " ")
    .trim();
  const fullTitle = `${compactCompany} - ${compactContact} - Research`;
  if (fullTitle.length <= 50) return fullTitle;
  return `${fullTitle.slice(0, 47).trimEnd()}...`;
}

async function createGoogleDocTab(args, leadContext, content) {
  if (!args.docWebhookUrl) return null;

  const payload = {
    secret: args.docWebhookSecret,
    tabTitle: buildDocTabTitle(leadContext),
    content,
  };

  const response = await fetch(args.docWebhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const bodyText = await response.text();
  let body;
  try {
    body = JSON.parse(bodyText);
  } catch {
    throw new Error(`Doc webhook returned non-JSON response (${response.status}): ${bodyText.slice(0, 500)}`);
  }

  if (!response.ok || !body.ok) {
    throw new Error(`Doc webhook failed (${response.status}): ${bodyText.slice(0, 1000)}`);
  }

  writeText(path.join(leadContext.outputDir, "google-doc-tab.json"), JSON.stringify(body, null, 2));
  console.log(`Google Doc tab created: ${body.tabTitle} (${body.tabId})`);
  return body;
}

async function waitForResearchResponseWithTiers(page, tierWaitsMs, stableMs, waitContext = {}, maxWaitMs = 250000) {
  const assistantCountBefore = typeof waitContext === "number" ? waitContext : waitContext.assistantCountBefore || 0;
  const responseActionCountBefore = typeof waitContext === "number" ? 0 : waitContext.responseActionCountBefore || 0;
  const startedAt = Date.now();

  for (let index = 0; index < tierWaitsMs.length; index += 1) {
    const waitMs = tierWaitsMs[index];
    console.log(`Waiting tier ${index + 1}/${tierWaitsMs.length}: ${Math.round(waitMs / 1000)}s`);
    await page.waitForTimeout(waitMs);
  }

  while (Date.now() - startedAt < maxWaitMs) {
    if (await hasFinishedResponseSignal(page, assistantCountBefore, responseActionCountBefore)) {
      const beforeQuiet = await latestAssistantState(page);
      console.log(
        `Finished controls detected on latest assistant turn; confirming ${Math.round(stableMs / 1000)}s quiet window ` +
          `(turns=${beforeQuiet.count}, chars=${beforeQuiet.textLength})`
      );
      await page.waitForTimeout(stableMs);
      if (await hasFinishedResponseSignal(page, assistantCountBefore, responseActionCountBefore)) {
        const afterQuiet = await latestAssistantState(page);
        if (afterQuiet.count !== beforeQuiet.count || afterQuiet.textLength !== beforeQuiet.textLength) {
          console.log(
            `Response changed during quiet window; continuing wait ` +
              `(turns ${beforeQuiet.count}->${afterQuiet.count}, chars ${beforeQuiet.textLength}->${afterQuiet.textLength})`
          );
          continue;
        }
        const latest = await getLatestSubstantialAssistantMessage(page, assistantCountBefore);
        if (latest) return latest;
      }
    }
    await page.waitForTimeout(5000);
  }

  throw new Error(`Timed out after ${maxWaitMs}ms without seeing finished response controls`);
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  if (args.loginOnly) {
    const browserSession = await openBrowser(args);
    const page = browserSession.context.pages()[0] || (await browserSession.context.newPage());
    await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
    console.log("ChatGPT is open. Sign in in the Chrome window, then press Enter here to close setup.");
    await new Promise((resolve) => process.stdin.once("data", resolve));
    await browserSession.close();
    return;
  }

  const outputRoot = path.resolve(expandHome(args.outputDir));
  const rows = args.sheetUrl ? fetchApprovedRowsFromGoogleSheet(args) : await readLeadRows(args.leads, args.sheet);
  const selectedLeads = args.sheetUrl && !args.prospectId ? rows : selectLeads(rows, args);
  const leads = args.limit ? selectedLeads.slice(0, args.limit) : selectedLeads;

  if (leads.length === 0) {
    console.log(args.approvedOnly ? "No approved leads found to process." : "No lead found to process.");
    return;
  }

  if (args.dryRun) {
    for (const [index, lead] of leads.entries()) {
      const leadContext = prepareLeadContext(lead, args, index);
      writeText(path.join(leadContext.outputDir, "lead.json"), JSON.stringify(lead, null, 2));
      writeText(path.join(leadContext.outputDir, "research-prompt.txt"), leadContext.prompt);
      console.log(`Dry run ${index + 1}/${leads.length}: prompt saved to ${path.join(leadContext.outputDir, "research-prompt.txt")}`);
    }
    return;
  }

  const browserSession = await openBrowser(args);

  try {
    const page = await getChatGptPage(browserSession.context);
    for (const [index, lead] of leads.entries()) {
      const leadContext = prepareLeadContext(lead, args, index);
      writeText(path.join(leadContext.outputDir, "lead.json"), JSON.stringify(lead, null, 2));
      writeText(path.join(leadContext.outputDir, "research-prompt.txt"), leadContext.prompt);

      console.log(`Processing ${index + 1}/${leads.length}: ${leadContext.company} / ${leadContext.contactName}`);
      let response = "";
      if (args.recaptureCurrent) {
        response = await copyLatestAssistantResponse(page).catch(async (error) => {
          console.log(`Copy-button capture failed; falling back to DOM text: ${error.message}`);
          return await getLatestSubstantialAssistantMessage(page, 0);
        });
        if (!response) throw new Error("No substantial assistant response found in the current ChatGPT tab");
      } else {
        let lastError = null;
        for (let attempt = 1; attempt <= args.sendRetries; attempt += 1) {
          try {
            console.log(`Opening fresh ChatGPT chat (attempt ${attempt}/${args.sendRetries})`);
            await openFreshChat(page);
            const waitContext = await sendPrompt(page, leadContext.prompt);
            await waitForAssistantTurnStarted(page, waitContext.assistantCountBefore, args.assistantStartTimeoutMs);
            await waitForResearchResponseWithTiers(page, args.tierWaitsMs, args.stableMs, waitContext, args.maxWaitMs);
            response = await copyLatestAssistantResponse(page).catch(async (error) => {
              console.log(`Copy-button capture failed; falling back to DOM text: ${error.message}`);
              return await getLatestSubstantialAssistantMessage(page, waitContext.assistantCountBefore);
            });
            break;
          } catch (error) {
            lastError = error;
            console.log(`Attempt ${attempt} failed: ${error.message}`);
            if (attempt === args.sendRetries) throw lastError;
          }
        }
      }
      writeText(leadContext.researchResponsePath, response);

      const chatgptUrl = page.url();
      writeText(path.join(leadContext.outputDir, "chatgpt-url.txt"), chatgptUrl);
      const docTab = await createGoogleDocTab(args, leadContext, response);
      updateGoogleSheetResult(args, leadContext, chatgptUrl, docTab);
      appendOutputIndex(outputRoot, {
        "Prospect ID": leadContext.prospectId,
        "Company": leadContext.company,
        "Company Website": leadContext.companyWebsite,
        "Contact Name": leadContext.contactName,
        "Contact Linkedin": leadContext.contactLinkedin,
        "Approval": getField(lead, "approval") || "TRUE",
        "ChatGPT Conversation URL": chatgptUrl,
        "Research summary": response,
        "Google Doc ID": docTab?.documentId || docTab?.docId || args.googleDocId,
        "Google Doc URL": docTab?.documentUrl || docTab?.docUrl || args.googleDocUrl,
        "Google Doc Tab ID": docTab?.tabId || "",
        "Google Doc Tab Title": docTab?.tabTitle || "",
        "Message draft": getField(lead, "messageDraft"),
      });

      console.log(`Research response saved to ${leadContext.researchResponsePath}`);
      console.log(`ChatGPT URL saved to ${path.join(leadContext.outputDir, "chatgpt-url.txt")}`);
      if (docTab) console.log(`Google Doc tab metadata saved to ${path.join(leadContext.outputDir, "google-doc-tab.json")}`);
    }
  } finally {
    await browserSession.close();
  }
}

function prepareLeadContext(lead, args, index = 0) {
  const prompt = buildPrompt(lead, args.template);
  const prospectId = getField(lead, "prospectId") || `row-${index + 1}`;
  const companyWebsite = getField(lead, "companyWebsite");
  const company = getField(lead, "company") || companyWebsite;
  const contactName = getField(lead, "contactName");
  const contactLinkedin = getField(lead, "contactLinkedin");
  const rowNumber = Number(lead._row_number || 0);
  const runSlug = `${slugify(prospectId)}-${slugify(company)}-${slugify(contactName)}`;
  const outputDir = path.resolve(expandHome(args.outputDir), runSlug);

  return {
    prompt,
    prospectId,
    company,
    companyWebsite,
    contactName,
    contactLinkedin,
    rowNumber,
    outputDir,
    researchResponsePath: path.join(outputDir, "research-response.md"),
  };
}

async function getChatGptPage(context) {
  const pages = context.pages();
  const chatPage = pages.find((page) => page.url().startsWith("https://chatgpt.com/"));
  if (chatPage) {
    await chatPage.bringToFront().catch(() => {});
    return chatPage;
  }
  const page = pages[0] || (await context.newPage());
  await page.bringToFront().catch(() => {});
  return page;
}

run()
  .then(() => {
    process.exit(0);
  })
  .catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
