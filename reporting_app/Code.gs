const DEFAULT_CONFIG = {
  spreadsheetId: '',
  sourceTabName: 'Pre-final',
  finalTabName: 'Final',
  reviewTabName: 'Lead Review',
  copyTabName: 'Copy of Pre-final',
  sanitizeLogTabName: 'Sanitize Log',
  comparisonSpreadsheetId: '',
  comparisonSheetGid: '',
  obfSpreadsheetId: '',
  prospectsTabName: 'Prospects',
  outreachControlTabName: 'Outreach Control',
  outreachLogTabName: 'Outreach Log',
  pipelineTabName: 'Pipeline',
  messagingTabName: 'Messaging',
  messagingDraftsTabName: 'Messaging Drafts',
  messagingTemplatesTabName: 'Messaging Templates',
  docWebhookUrl: '',
  docWebhookSecret: '',
  dailyMetricsTabName: 'Daily Metrics',
  pageSize: 500,
  reviewPageSize: 150,
  sanitizeLogPageSize: 200,
  outreachLogPageSize: 500,
};

const CONFIG_PROPERTY_MAP = {
  spreadsheetId: 'LEADS_SPREADSHEET_ID',
  comparisonSpreadsheetId: 'COMPARISON_SPREADSHEET_ID',
  comparisonSheetGid: 'COMPARISON_SHEET_GID',
  obfSpreadsheetId: 'OBF_SPREADSHEET_ID',
  docWebhookUrl: 'DOC_WEBHOOK_URL',
  docWebhookSecret: 'DOC_WEBHOOK_SECRET',
};

const CONFIG = loadRuntimeConfig_();

function loadRuntimeConfig_() {
  const properties = PropertiesService.getScriptProperties().getProperties();
  const config = Object.assign({}, DEFAULT_CONFIG);
  Object.keys(CONFIG_PROPERTY_MAP).forEach((key) => {
    const value = String(properties[CONFIG_PROPERTY_MAP[key]] || '').trim();
    if (value) {
      config[key] = value;
    }
  });
  return config;
}

function getRequiredConfig_(key) {
  const value = String(CONFIG[key] || '').trim();
  if (!value) {
    const property = CONFIG_PROPERTY_MAP[key] || key;
    throw new Error(`Missing required Script Property: ${property}. See SCRIPT_PROPERTIES.example.md.`);
  }
  return value;
}

const SANITIZE_LOG_HEADERS = [
  'Run At',
  'Run ID',
  'Action',
  'Lead ID',
  'Company',
  'New P1 Name',
  'Old P1 Name',
  'Match Reason',
];

const SOURCE_REQUIRED_COLUMNS = [
  'ID',
  'Company',
  'Website',
  'Company LinkedIn',
  'Emp Count',
  'Source Tab',
  'Use',
  'P1 Name',
  'P1 Title',
  'P1 LinkedIn',
  'P1 Email',
  'P2 Name',
  'P2 Title',
  'P2 LinkedIn',
  'P2 Email',
];

const FINAL_REQUIRED_COLUMNS = [
  'ID',
  'Company',
  'Website',
  'Company LinkedIn',
  'Emp Count',
  'Source Tab',
  'Use',
  'P1 Name',
  'P1 Title',
  'P1 LinkedIn',
  'P1 Email',
  'P1 Activity',
  'P2 Name',
  'P2 Title',
  'P2 LinkedIn',
  'P2 Email',
  'P2 Activity',
  'Category',
];

const ACTIVITY_VALUES = ['', 'Very active', 'Active', 'Not active'];
const P1_COLUMNS = ['P1 Name', 'P1 Title', 'P1 LinkedIn', 'P1 Email'];
const P2_COLUMNS = ['P2 Name', 'P2 Title', 'P2 LinkedIn', 'P2 Email'];
const NAME_COLUMN_CANDIDATES = [
  'Name',
  'Full Name',
  'Person Name',
  'P1 Name',
  'Lead Name',
  'First Name Last Name',
];
const LINKEDIN_COLUMN_CANDIDATES = [
  'LinkedIn',
  'LinkedIn URL',
  'LinkedIn Profile',
  'LinkedIn Link',
  'Profile URL',
  'Profile',
  'URL',
];
const CATEGORY_PRIORITY = {
  Hyper: 0,
  High: 1,
  'Alpha-medium': 2,
  Medium: 3,
  Low: 4,
};

const PROSPECTS_REQUIRED_COLUMNS = [
  'ID',
  'Company',
  'Website',
  'Company LinkedIn',
  'Emp Count',
  'Source Tab',
  'P1 Name',
  'P1 Title',
  'P1 LinkedIn',
  'P1 Email',
  'P1 Activity',
  'P2 Name',
  'P2 Title',
  'P2 LinkedIn',
  'P2 Email',
  'P2 Activity',
  'Engaged Person',
  'Touch Method',
  'Outreach Status',
  'Outcome',
  'Date Queued',
  'Notes',
];

const OUTREACH_CONTROL_PROSPECTS_START_ROW = 'Prospects Start Row';

const OUTREACH_PROGRESS_VALUES = [
  'Connected',
  'First Message',
  'FU-1',
  'FU-2',
  'FU-3',
  'FU-4',
  'FU-5',
  'FU-6',
  'FU-7',
  'FU-8',
  'FU-9',
  'FU-10',
  'Conn Request',
  'Withdrawn',
  'Request Withdrawn',
];

const OUTREACH_OUTCOME_VALUES = [
  'Pending',
  'No Response',
  'Not Interested',
  'Interested',
  'Meeting Set',
  'Audit Requested',
  'Audit Sent',
  'Proposal Requested',
  'Won',
  'Lost',
];

const OUTREACH_LOG_REQUIRED_COLUMNS = [
  'Prospect ID',
  'Company',
  'Person Engaged',
  'Contact Name',
  'Current Progress',
  'Touch Method',
  'Outcome',
  'Last Action Date',
  'Sent At',
  'Days Left',
  'Notes',
];

const PIPELINE_REQUIRED_COLUMNS = [
  'Prospect ID',
  'Company',
  'Person Engaged',
  'Contact Name',
  'Current Stage',
  'Current Progress',
  'Last Action Date',
  'Next Action',
  'Next Action Due',
];

const MESSAGING_REQUIRED_COLUMNS = [
  'Prospect ID',
  'Company',
  'Company Website',
  'Contact Name',
  'Contact Linkedin',
];

const MESSAGING_PAGE_COLUMNS = MESSAGING_REQUIRED_COLUMNS.concat([
  'ChatGPT Conversation URL',
  'Research summary',
  'Google Doc ID',
  'Google Doc URL',
  'Google Doc Tab ID',
  'Google Doc Tab Title',
]);

const MESSAGE_DRAFT_REQUIRED_COLUMNS = [
  'Draft ID',
  'Prospect ID',
  'Stage',
  'Template ID',
  'Draft Message',
  'Drafted At',
  'Status',
];

const PIPELINE_SEQUENCE = {
  Connected: { nextAction: 'First Message', days: 1 },
  'First Message': { nextAction: 'FU-1', days: 3 },
  'FU-1': { nextAction: 'FU-2', days: 4 },
  'FU-2': { nextAction: 'FU-3', days: 3 },
  'FU-3': { nextAction: 'FU-4', workingDays: 1 },
  'FU-4': { nextAction: 'FU-5', days: 4 },
  'FU-5': { nextAction: 'FU-6', days: 5 },
  'FU-6': { nextAction: 'FU-7', days: 6 },
  'FU-7': { nextAction: 'FU-8', days: 7 },
  'FU-8': { nextAction: 'FU-9', days: 8 },
  'FU-9': { nextAction: 'FU-10', days: 12 },
};

const PIPELINE_PROGRESS_VALUES = [
  'Connected',
  'First Message',
  'FU-1',
  'FU-2',
  'FU-3',
  'FU-4',
  'FU-5',
  'FU-6',
  'FU-7',
  'FU-8',
  'FU-9',
  'FU-10',
  'Fail',
];

const PIPELINE_STAGE_VALUES = [
  'Pending',
  'First Msg',
  'Follow-ups',
  'Audit Requested',
  'Audit Sent',
  'Audit Acknowledged',
  'Call Scheduled',
  'Call Completed',
  'Proposal Sent',
  'Negotiating',
  'Closed Won',
  'Closed Lost',
  'Stalled',
];

const DAILY_METRICS_REQUIRED_COLUMNS = [
  'Date',
  'Conn Sent',
  'Conn Target',
  'Accepted',
  'Total Conn Sent',
  'Total Accepted',
  'All-Time Acceptance Rate',
  'First Msgs Sent',
  'Follow-Ups Sent',
  'Replies',
  'Reply Rate',
  'Comments Posted',
  'Requests Withdrawn',
  'New Pipeline',
  'Calls Booked',
  'Audits Sent',
  'Proposals Sent',
  'Notes',
];

const WEEKLY_METRIC_SUM_COLUMNS = [
  'Conn Sent',
  'Conn Target',
  'Accepted',
  'First Msgs Sent',
  'Follow-Ups Sent',
  'Replies',
  'Comments Posted',
  'Requests Withdrawn',
  'New Pipeline',
  'Calls Booked',
  'Audits Sent',
  'Proposals Sent',
];

const REVIEW_REQUIRED_COLUMNS = [
  'Run ID',
  'Company Name',
  'Company Website',
  'Emp Count',
  'Approved',
  'Use',
];

const REVIEW_USE_VALUES = [
  '',
  'Potential leads',
  'Case study worthy',
  'Not bad, Not good',
  'Not a lead',
  'Exclude',
];

function doGet(e) {
  const view = String((e && e.parameter && e.parameter.view) || '').trim().toLowerCase();
  const initialView = view === 'review' ? 'lead_review' : view;
  const title = initialView === 'pipeline'
    ? 'Pipeline'
    : initialView === 'messaging'
      ? 'Messaging'
    : initialView === 'metrics'
      ? 'Metrics'
    : initialView === 'outreach_log'
      ? 'Outreach Log'
      : initialView === 'lead_review'
        ? 'Lead Review'
        : 'Lead Activity';
  const template = HtmlService.createTemplateFromFile('Index');
  template.initialView = ['lead_review', 'sanitize_log', 'manual_requests', 'outreach_log', 'pipeline', 'messaging', 'metrics'].indexOf(initialView) !== -1
    ? initialView
    : 'lead_activity';
  template.webAppUrl = ScriptApp.getService().getUrl();
  return template.evaluate()
    .setTitle(title)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, viewport-fit=cover')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

function getLeadPayload(options) {
  const opts = options || {};
  const sourceSheet = getSourceSheet_();
  const finalSheet = getFinalSheet_();
  const values = sourceSheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    return buildPayload_([], sourceSheet, finalSheet, {});
  }

  const headers = values[0].map((value) => String(value).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, SOURCE_REQUIRED_COLUMNS);
  const finalIds = getFinalIds_(finalSheet);
  const movedIds = getCopyIds_();

  const mode = String(opts.mode || 'pending').trim().toLowerCase();
  const search = String(opts.search || '').trim().toLowerCase();
  const requestedLimit = Number(opts.limit || 0);
  const limit = requestedLimit > 0 ? Math.max(1, requestedLimit) : Number.POSITIVE_INFINITY;
  const leads = [];

  for (let r = 1; r < values.length; r++) {
    const row = values[r];
    const lead = leadFromRow_(row, index, r + 1);
    if (!lead.id && !lead.company) {
      continue;
    }
    if (!lead.p1.linkedin && !lead.p2.linkedin) {
      continue;
    }
    if (lead.id && movedIds.has(lead.id)) {
      continue;
    }
    const alreadyFinal = finalIds.has(lead.id);
    if (mode === 'pending' && alreadyFinal) {
      continue;
    }
    lead.alreadyFinal = alreadyFinal;
    if (search && !leadMatchesSearch_(lead, search)) {
      continue;
    }
    leads.push(lead);
    if (Number.isFinite(limit) && leads.length >= limit) {
      break;
    }
  }

  return buildPayload_(leads, sourceSheet, finalSheet, {
    mode,
    search,
    limit: Number.isFinite(limit) ? limit : '',
    headers,
  });
}

function submitLeadToFinal(rowNumber, activities) {
  const sourceSheet = getSourceSheet_();
  const finalSheet = getFinalSheet_();
  const sourceHeaders = sourceSheet.getRange(1, 1, 1, sourceSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const finalHeaders = finalSheet.getRange(1, 1, 1, finalSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const sourceIndex = buildHeaderIndex_(sourceHeaders);
  const finalIndex = buildHeaderIndex_(finalHeaders);
  requireColumns_(sourceIndex, SOURCE_REQUIRED_COLUMNS);
  requireColumns_(finalIndex, FINAL_REQUIRED_COLUMNS);

  const row = Number(rowNumber);
  if (!Number.isInteger(row) || row < 2 || row > sourceSheet.getLastRow()) {
    throw new Error(`Invalid row number: ${rowNumber}`);
  }

  const sourceValues = sourceSheet.getRange(row, 1, 1, sourceSheet.getLastColumn()).getDisplayValues()[0];
  const sourceLead = leadFromRow_(sourceValues, sourceIndex, row);
  const ranked = rankLeadForFinal_(sourceLead, {
    p1Activity: normalizeActivityValue_((activities || {}).p1Activity),
    p2Activity: normalizeActivityValue_((activities || {}).p2Activity),
  });
  const finalRow = findFinalRowById_(finalSheet, finalIndex, ranked.ID);
  const targetRow = finalRow || Math.max(2, finalSheet.getLastRow() + 1);
  const payload = finalHeaders.map((header) => ranked[header] || '');
  finalSheet.getRange(targetRow, 1, 1, finalHeaders.length).setValues([payload]);
  sortFinal_(finalSheet, finalHeaders);

  return {
    ok: true,
    sourceRowNumber: row,
    finalRowNumber: targetRow,
    updatedExisting: Boolean(finalRow),
    id: ranked.ID,
    company: ranked.Company,
    category: ranked.Category,
    p1Activity: ranked['P1 Activity'],
    p2Activity: ranked['P2 Activity'],
    updatedAt: new Date().toISOString(),
  };
}

function submitLeadBatchToFinal(items, options) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return {
      ok: true,
      processed: 0,
      results: [],
      workflowState: getWorkflowState_(),
      updatedAt: new Date().toISOString(),
    };
  }

  const sourceSheet = getSourceSheet_();
  const finalSheet = getFinalSheet_();
  const sourceHeaders = sourceSheet.getRange(1, 1, 1, sourceSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const finalHeaders = finalSheet.getRange(1, 1, 1, finalSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const sourceIndex = buildHeaderIndex_(sourceHeaders);
  const finalIndex = buildHeaderIndex_(finalHeaders);
  requireColumns_(sourceIndex, SOURCE_REQUIRED_COLUMNS);
  requireColumns_(finalIndex, FINAL_REQUIRED_COLUMNS);

  const results = [];
  rows.forEach((item) => {
    const row = Number(item && item.rowNumber);
    if (!Number.isInteger(row) || row < 2 || row > sourceSheet.getLastRow()) {
      throw new Error(`Invalid row number: ${item && item.rowNumber}`);
    }
    const sourceValues = sourceSheet.getRange(row, 1, 1, sourceSheet.getLastColumn()).getDisplayValues()[0];
    const sourceLead = leadFromRow_(sourceValues, sourceIndex, row);
    const ranked = rankLeadForFinal_(sourceLead, {
      p1Activity: normalizeActivityValue_((item.activities || {}).p1Activity),
      p2Activity: normalizeActivityValue_((item.activities || {}).p2Activity),
    });
    const finalRow = findFinalRowById_(finalSheet, finalIndex, ranked.ID);
    const targetRow = finalRow || Math.max(2, finalSheet.getLastRow() + 1);
    const payload = finalHeaders.map((header) => ranked[header] || '');
    finalSheet.getRange(targetRow, 1, 1, finalHeaders.length).setValues([payload]);
    results.push({
      sourceRowNumber: row,
      finalRowNumber: targetRow,
      updatedExisting: Boolean(finalRow),
      id: ranked.ID,
      company: ranked.Company,
      category: ranked.Category,
    });
  });

  sortFinal_(finalSheet, finalHeaders);
  const nextState = (options && options.finalChunk)
    ? saveWorkflowState_({
      bridgeFinalDone: true,
      bridgeFinalAt: new Date().toISOString(),
      bridgeFinalLastProcessed: results.length,
    })
    : getWorkflowState_();

  return {
    ok: true,
    processed: results.length,
    results,
    workflowState: nextState,
    updatedAt: new Date().toISOString(),
  };
}

function runSanitize() {
  const blocked = readBlockedSet_();
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  const finalSheet = getFinalSheet_();
  const copySheet = spreadsheet.getSheetByName(CONFIG.copyTabName);
  if (!copySheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.copyTabName}`);
  }

  const values = finalSheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    const workflowState = saveWorkflowState_({
      sanitizeDone: true,
      sanitizeAt: new Date().toISOString(),
      sanitizeRowsSeen: 0,
      sanitizeRowsSwapped: 0,
      sanitizeRowsMoved: 0,
    });
    return {
      ok: true,
      sourceTab: CONFIG.finalTabName,
      comparisonTab: blocked.sheetName,
      comparisonNameColumn: blocked.nameColumn,
      comparisonLinkedinColumn: blocked.linkedinColumn,
      comparisonNamesLoaded: blocked.names.size,
      comparisonLinkedinsLoaded: blocked.linkedins.size,
      rowsSeen: 0,
      rowsSwapped: 0,
      rowsMovedToCopy: 0,
      finalRowsAfter: 0,
      workflowState,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = values[0].map((header) => String(header).trim());
  const finalIndex = buildHeaderIndex_(headers);
  requireColumns_(finalIndex, ['ID', 'Company', 'P1 Name', 'P1 LinkedIn', 'P2 Name', 'P2 LinkedIn']);
  const swapPairs = buildSwapPairs_(headers);

  const copyHeaders = copySheet.getRange(1, 1, 1, copySheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());

  const keptRows = [];
  const movedRows = [];
  const swappedRows = [];
  const logEntries = [];
  const runAt = new Date().toISOString();
  const runId = Utilities.getUuid().slice(0, 8);

  for (let r = 1; r < values.length; r++) {
    const row = values[r];
    if (!isMeaningfulLeadRow_(row, finalIndex)) {
      continue;
    }
    const reason = findP1BlockReason_(row, finalIndex, blocked);
    if (reason) {
      const oldP1Name = cell_(row, finalIndex, 'P1 Name');
      const leadId = cell_(row, finalIndex, 'ID');
      const company = cell_(row, finalIndex, 'Company');
      if (rowHasP2_(row, finalIndex)) {
        const swapped = swapColumns_(row, finalIndex, swapPairs);
        swappedRows.push(swapped);
        keptRows.push(swapped);
        logEntries.push({
          runAt,
          runId,
          action: 'Swapped',
          leadId,
          company,
          newP1Name: cell_(swapped, finalIndex, 'P1 Name'),
          oldP1Name,
          reason,
        });
      } else {
        movedRows.push(row);
        logEntries.push({
          runAt,
          runId,
          action: 'Moved',
          leadId,
          company,
          newP1Name: '',
          oldP1Name,
          reason,
        });
      }
    } else {
      keptRows.push(row);
    }
  }

  appendRowsToCopy_(copySheet, copyHeaders, headers, movedRows);
  rewriteSheetBody_(finalSheet, headers.length, keptRows);
  sortFinal_(finalSheet, headers);
  appendSanitizeLogEntries_(logEntries);

  const workflowState = saveWorkflowState_({
    sanitizeDone: true,
    sanitizeAt: new Date().toISOString(),
    sanitizeRowsSeen: keptRows.length + movedRows.length,
    sanitizeRowsSwapped: swappedRows.length,
    sanitizeRowsMoved: movedRows.length,
  });

  return {
    ok: true,
    sourceTab: CONFIG.finalTabName,
    comparisonTab: blocked.sheetName,
    comparisonNameColumn: blocked.nameColumn,
    comparisonLinkedinColumn: blocked.linkedinColumn,
    comparisonNamesLoaded: blocked.names.size,
    comparisonLinkedinsLoaded: blocked.linkedins.size,
    rowsSeen: keptRows.length + movedRows.length,
    rowsSwapped: swappedRows.length,
    rowsMovedToCopy: movedRows.length,
    finalRowsAfter: keptRows.length,
    workflowState,
    updatedAt: new Date().toISOString(),
  };
}

function findP1BlockReason_(row, index, blocked) {
  const url = normalizeLinkedInForMatch_(cell_(row, index, 'P1 LinkedIn'));
  if (url && blocked.linkedins.has(url)) {
    return 'LinkedIn URL';
  }
  const name = normalizeName_(cell_(row, index, 'P1 Name'));
  if (name && blocked.names.has(name)) {
    return 'Name';
  }
  return '';
}

function buildSwapPairs_(headers) {
  const headerSet = {};
  headers.forEach((header) => { headerSet[header] = true; });
  const pairs = [];
  headers.forEach((header) => {
    const match = /^P1 (.+)$/.exec(header);
    if (!match) return;
    const p2Header = `P2 ${match[1]}`;
    if (headerSet[p2Header]) {
      pairs.push([header, p2Header]);
    }
  });
  return pairs;
}

function swapColumns_(row, index, swapPairs) {
  const next = row.slice();
  swapPairs.forEach((pair) => {
    const p1Column = pair[0];
    const p2Column = pair[1];
    const p1Value = next[index[p1Column]] || '';
    next[index[p1Column]] = next[index[p2Column]] || '';
    next[index[p2Column]] = p1Value;
  });
  return next;
}

function isMeaningfulLeadRow_(row, index) {
  return ['ID', 'Company', 'P1 Name', 'P1 LinkedIn', 'P2 Name', 'P2 LinkedIn'].some((column) => cell_(row, index, column));
}

function bridgeFinalToProspects() {
  const finalSheet = getFinalSheet_();
  const prospectsSheet = getProspectsSheet_();
  const finalValues = finalSheet.getDataRange().getDisplayValues();
  if (finalValues.length < 2) {
    return {
      ok: true,
      processed: 0,
      skippedExisting: 0,
      appended: 0,
      workflowState: getWorkflowState_(),
      updatedAt: new Date().toISOString(),
    };
  }

  const finalHeaders = finalValues[0].map((header) => String(header).trim());
  const prospectsHeaders = prospectsSheet.getRange(1, 1, 1, prospectsSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const finalIndex = buildHeaderIndex_(finalHeaders);
  const prospectsIndex = buildHeaderIndex_(prospectsHeaders);
  requireColumns_(finalIndex, FINAL_REQUIRED_COLUMNS);
  requireColumns_(prospectsIndex, PROSPECTS_REQUIRED_COLUMNS);

  const existingIds = getProspectsIds_(prospectsSheet, prospectsIndex);
  const payload = [];
  let skippedExisting = 0;
  let skippedUnranked = 0;
  let startRow = null;
  let outreachControlUpdate = null;
  for (let r = 1; r < finalValues.length; r++) {
    const row = finalValues[r];
    const id = cell_(row, finalIndex, 'ID');
    const company = cell_(row, finalIndex, 'Company');
    if (!id && !company) {
      continue;
    }
    const category = cell_(row, finalIndex, 'Category');
    if (!category) {
      skippedUnranked += 1;
      continue;
    }
    if (id && existingIds.has(id)) {
      skippedExisting += 1;
      continue;
    }
    payload.push(buildProspectRow_(row, finalIndex, prospectsHeaders));
    if (id) {
      existingIds.add(id);
    }
  }

  if (payload.length) {
    startRow = firstAppendRow_(prospectsSheet, prospectsHeaders.length);
    prospectsSheet.getRange(startRow, 1, payload.length, prospectsHeaders.length).setValues(payload);
    try {
      outreachControlUpdate = recordOutreachControlProspectsStartRow_(startRow);
    } catch (error) {
      outreachControlUpdate = { ok: false, error: String(error && error.message ? error.message : error) };
    }
  }

  const nextState = saveWorkflowState_({
    bridgeProspectsDone: true,
    bridgeProspectsAt: new Date().toISOString(),
    bridgeProspectsLastAppended: payload.length,
    bridgeProspectsLastStartRow: startRow,
    bridgeProspectsLastSkippedExisting: skippedExisting,
  });

  return {
    ok: true,
    processed: payload.length + skippedExisting + skippedUnranked,
    skippedExisting,
    skippedUnranked,
    appended: payload.length,
    startRow,
    outreachControlUpdate,
    workflowState: nextState,
    updatedAt: new Date().toISOString(),
  };
}

function getManualRequestsPayload(options) {
  const opts = options || {};
  const filter = String(opts.filter || 'all').trim().toLowerCase();
  const limit = Math.max(1, Math.min(Number(opts.limit || 500), 1000));

  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return buildManualRequestsPayload_([], { all: 0, new: 0, connection_sent: 0, connected: 0 }, filter);
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['ID', 'Company', 'P1 Name', 'P1 LinkedIn', 'P2 Name', 'P2 LinkedIn', 'Outreach Status']);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const counts = { all: 0, new: 0, connection_sent: 0, connected: 0 };
  const leads = [];

  for (let i = 0; i < values.length; i++) {
    const row = values[i];
    const id = cell_(row, index, 'ID');
    const company = cell_(row, index, 'Company');
    if (!id && !company) {
      continue;
    }
    const status = cell_(row, index, 'Outreach Status');
    const statusKey = classifyOutreachStatus_(status);
    counts.all += 1;
    if (statusKey === 'new') counts.new += 1;
    else if (statusKey === 'connection_sent') counts.connection_sent += 1;
    else if (statusKey === 'connected') counts.connected += 1;

    if (filter !== 'all' && filter !== statusKey) {
      continue;
    }
    if (leads.length >= limit) {
      continue;
    }
    leads.push({
      rowNumber: i + 2,
      id,
      company,
      website: normalizeUrl_(cell_(row, index, 'Website')),
      companyLinkedIn: normalizeUrl_(cell_(row, index, 'Company LinkedIn')),
      empCount: cell_(row, index, 'Emp Count'),
      outreachStatus: status,
      statusKey,
      touchMethod: cell_(row, index, 'Touch Method'),
      outcome: cell_(row, index, 'Outcome'),
      notes: cell_(row, index, 'Notes'),
      engagedPerson: cell_(row, index, 'Engaged Person'),
      p1: {
        name: cell_(row, index, 'P1 Name'),
        title: cell_(row, index, 'P1 Title'),
        linkedin: normalizeUrl_(cell_(row, index, 'P1 LinkedIn')),
        email: cell_(row, index, 'P1 Email'),
        activity: cell_(row, index, 'P1 Activity'),
      },
      p2: {
        name: cell_(row, index, 'P2 Name'),
        title: cell_(row, index, 'P2 Title'),
        linkedin: normalizeUrl_(cell_(row, index, 'P2 LinkedIn')),
        email: cell_(row, index, 'P2 Email'),
        activity: cell_(row, index, 'P2 Activity'),
      },
    });
  }

  return buildManualRequestsPayload_(leads, counts, filter);
}

function updateManualRequests(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return { ok: true, processed: 0, updatedAt: new Date().toISOString() };
  }
  const sheet = getProspectsSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Outreach Status', 'Engaged Person']);

  let pushedToLog = 0;
  const lastRow = sheet.getLastRow();
  rows.forEach((item) => {
    const rowNumber = Number(item && item.rowNumber);
    if (!Number.isInteger(rowNumber) || rowNumber < 2 || rowNumber > lastRow) {
      throw new Error(`Invalid prospect row number: ${item && item.rowNumber}`);
    }

    if (Object.prototype.hasOwnProperty.call(item, 'engagedPerson')) {
      sheet.getRange(rowNumber, index['Engaged Person'] + 1)
        .setValue(String(item.engagedPerson || '').trim());
    }

    if (Object.prototype.hasOwnProperty.call(item, 'outreachStatus')) {
      const oldStatus = cell_(sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0], index, 'Outreach Status');
      const nextStatus = String(item.outreachStatus || '').trim();
      const nextStatusKey = nextStatus.toLowerCase();
      sheet.getRange(rowNumber, index['Outreach Status'] + 1).setValue(nextStatus);
      if (classifyOutreachStatus_(oldStatus) !== classifyOutreachStatus_(nextStatus)) {
        if (nextStatusKey === 'connection sent') {
          incrementDailyMetrics_({ 'Conn Sent': 1 });
        } else if (nextStatusKey === 'connected') {
          incrementDailyMetrics_({ Accepted: 1 });
        }
      }

      if (nextStatusKey === 'connection sent' || nextStatusKey === 'connected') {
        const prospectRow = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
        const pushResult = pushToOutreachLog_(prospectRow, index, {
          currentProgress: nextStatusKey === 'connected' ? 'Connected' : 'Conn Request',
          outcome: nextStatusKey === 'connected' ? 'Pending' : '',
          stampSentAt: nextStatusKey === 'connection sent',
        });
        if (pushResult && pushResult.pipelineInserted) {
          incrementDailyMetrics_({ 'New Pipeline': 1 });
        }
        pushedToLog += 1;
      }
    }
  });

  return {
    ok: true,
    processed: rows.length,
    pushedToOutreachLog: pushedToLog,
    updatedAt: new Date().toISOString(),
  };
}

function backfillConnectedProspectsToOutreachLog() {
  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {
      ok: true,
      scanned: 0,
      pushedToOutreachLog: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['ID', 'Outreach Status', 'Engaged Person']);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  let scanned = 0;
  let pushedToLog = 0;
  values.forEach((row) => {
    const id = cell_(row, index, 'ID');
    const status = cell_(row, index, 'Outreach Status');
    if (!id && !status) {
      return;
    }
    scanned += 1;
    if (String(status || '').trim().toLowerCase() !== 'connected') {
      return;
    }
    pushToOutreachLog_(row, index, {
      currentProgress: 'Connected',
      outcome: 'Pending',
      stampSentAt: false,
    });
    pushedToLog += 1;
  });

  return {
    ok: true,
    scanned,
    pushedToOutreachLog: pushedToLog,
    updatedAt: new Date().toISOString(),
  };
}

function backfillConnectedOutreachLogToPipeline() {
  const sheet = getOutreachLogSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {
      ok: true,
      scanned: 0,
      pushedToPipeline: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Prospect ID', 'Current Progress']);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  let scanned = 0;
  let pushedToPipeline = 0;
  values.forEach((row) => {
    const prospectId = cell_(row, index, 'Prospect ID');
    const progress = cell_(row, index, 'Current Progress');
    if (!prospectId && !progress) {
      return;
    }
    scanned += 1;
    if (classifyProgress_(progress) !== 'connected') {
      return;
    }
    upsertPipelineFromOutreachLog_(row, index);
    pushedToPipeline += 1;
  });

  return {
    ok: true,
    scanned,
    pushedToPipeline,
    updatedAt: new Date().toISOString(),
  };
}

function pushToOutreachLog_(prospectRow, prospectIndex, options) {
  const prospectId = cell_(prospectRow, prospectIndex, 'ID');
  if (!prospectId) {
    return { ok: false, pipelineInserted: false };
  }
  const opts = options || {};
  const currentProgress = String(opts.currentProgress || 'Conn Request').trim();
  const outcome = Object.prototype.hasOwnProperty.call(opts, 'outcome')
    ? String(opts.outcome || '').trim()
    : '';
  const shouldStampSentAt = opts.stampSentAt !== false;
  const sheet = getOutreachLogSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const logIndex = buildHeaderIndex_(headers);
  requireColumns_(logIndex, ['Prospect ID', 'Current Progress']);

  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  const existingRow = findOutreachLogRowById_(sheet, logIndex, prospectId);

  if (existingRow) {
    sheet.getRange(existingRow, logIndex['Current Progress'] + 1).setValue(currentProgress);
    if ('Outcome' in logIndex && outcome) {
      sheet.getRange(existingRow, logIndex['Outcome'] + 1).setValue(outcome);
    }
    const contactLinkedInHeader = contactLinkedInHeader_(logIndex);
    if (contactLinkedInHeader) {
      const contactLinkedInCell = sheet.getRange(existingRow, logIndex[contactLinkedInHeader] + 1);
      const existingContactLinkedIn = String(contactLinkedInCell.getDisplayValue() || '').trim();
      if (!existingContactLinkedIn) {
        contactLinkedInCell.setValue(contactLinkedInFromProspectRow_(prospectRow, prospectIndex, cell_(prospectRow, prospectIndex, 'Engaged Person')));
      }
    }
    if (shouldStampSentAt && 'Sent At' in logIndex) {
      const sentAtCell = sheet.getRange(existingRow, logIndex['Sent At'] + 1);
      const existingSentAt = String(sentAtCell.getDisplayValue() || '').trim();
      if (!existingSentAt) {
        sentAtCell.setValue(today);
      }
    }
    if ('Last Action Date' in logIndex) {
      sheet.getRange(existingRow, logIndex['Last Action Date'] + 1).setValue(today);
    }
    if (classifyProgress_(currentProgress) === 'connected') {
      const outreachRow = sheet.getRange(existingRow, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
      return upsertPipelineFromOutreachLog_(outreachRow, logIndex);
    }
    return { ok: true, pipelineInserted: false };
  }

  const engagedPerson = cell_(prospectRow, prospectIndex, 'Engaged Person');
  const contactName = engagedPerson === 'Person 2'
    ? cell_(prospectRow, prospectIndex, 'P2 Name')
    : cell_(prospectRow, prospectIndex, 'P1 Name');
  const contactLinkedIn = contactLinkedInFromProspectRow_(prospectRow, prospectIndex, engagedPerson);
  const mapped = {
    'Prospect ID': prospectId,
    'Company': cell_(prospectRow, prospectIndex, 'Company'),
    'Person Engaged': engagedPerson,
    'Contact Name': contactName,
    'Contact Linkedin': contactLinkedIn,
    'Contact LinkedIn': contactLinkedIn,
    'Current Progress': currentProgress,
    'Touch Method': cell_(prospectRow, prospectIndex, 'Touch Method'),
    'Outcome': outcome,
    'Last Action Date': today,
    'Sent At': shouldStampSentAt ? today : '',
    'Notes': cell_(prospectRow, prospectIndex, 'Notes'),
  };
  const startRow = firstAppendRow_(sheet, headers.length);
  const payload = headers.map((header) => (mapped[header] !== undefined ? mapped[header] : ''));
  writeMappedRowSkippingColumns_(sheet, startRow, headers, mapped, ['Days Left']);
  if (classifyProgress_(currentProgress) === 'connected') {
    return upsertPipelineFromOutreachLog_(payload, logIndex);
  }
  return { ok: true, pipelineInserted: false };
}

function contactLinkedInFromProspectRow_(prospectRow, prospectIndex, engagedPerson) {
  const linkedIn = String(engagedPerson || '').trim() === 'Person 2'
    ? cell_(prospectRow, prospectIndex, 'P2 LinkedIn')
    : cell_(prospectRow, prospectIndex, 'P1 LinkedIn');
  return normalizeUrl_(linkedIn);
}

function contactLinkedInFromProspectContact_(prospectContact, engagedPerson, contactName) {
  if (!prospectContact) {
    return '';
  }
  const engaged = String(engagedPerson || '').trim().toLowerCase();
  if (engaged === 'person 2' || engaged === 'p2') {
    return normalizeUrl_(prospectContact.p2Linkedin);
  }
  if (engaged === 'person 1' || engaged === 'p1') {
    return normalizeUrl_(prospectContact.p1Linkedin);
  }
  const contact = normalizePersonName_(contactName);
  if (contact && contact === normalizePersonName_(prospectContact.p2Name)) {
    return normalizeUrl_(prospectContact.p2Linkedin);
  }
  if (contact && contact === normalizePersonName_(prospectContact.p1Name)) {
    return normalizeUrl_(prospectContact.p1Linkedin);
  }
  return normalizeUrl_(prospectContact.p1Linkedin || prospectContact.p2Linkedin);
}

function normalizePersonName_(name) {
  return String(name || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function resolveProspectContactLinkedIn_(prospectId, engagedPerson, contactName) {
  return resolveContactLinkedInFromProspectLookup_(
    buildProspectContactsById_(),
    prospectId,
    engagedPerson,
    contactName,
  );
}

function findOutreachLogRowById_(sheet, logIndex, prospectId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return 0;
  }
  const idCol = logIndex['Prospect ID'] + 1;
  const values = sheet.getRange(2, idCol, lastRow - 1, 1).getDisplayValues();
  for (let i = 0; i < values.length; i++) {
    if (String(values[i][0] || '').trim() === prospectId) {
      return i + 2;
    }
  }
  return 0;
}

function upsertPipelineFromOutreachLog_(outreachRow, outreachIndex) {
  const prospectId = cell_(outreachRow, outreachIndex, 'Prospect ID');
  if (!prospectId) {
    return { ok: false, pipelineInserted: false };
  }

  const sheet = getPipelineSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const pipelineIndex = buildHeaderIndex_(headers);
  requireColumns_(pipelineIndex, PIPELINE_REQUIRED_COLUMNS);
  if (!contactLinkedInHeader_(pipelineIndex)) {
    throw new Error('Missing required columns: Contact Linkedin');
  }

  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  const contactLinkedInHeader = contactLinkedInHeader_(outreachIndex);
  const contactLinkedInFromLog = contactLinkedInHeader
    ? normalizeUrl_(cell_(outreachRow, outreachIndex, contactLinkedInHeader))
    : '';
  const contactLinkedIn = contactLinkedInFromLog || resolveProspectContactLinkedIn_(
    prospectId,
    cell_(outreachRow, outreachIndex, 'Person Engaged'),
    cell_(outreachRow, outreachIndex, 'Contact Name'),
  );
  const lastActionDate = cell_(outreachRow, outreachIndex, 'Last Action Date') || today;
  const schedule = buildPipelineNextStep_('Connected', lastActionDate);
  const mapped = {
    'Prospect ID': prospectId,
    Company: cell_(outreachRow, outreachIndex, 'Company'),
    'Person Engaged': cell_(outreachRow, outreachIndex, 'Person Engaged'),
    'Contact Name': cell_(outreachRow, outreachIndex, 'Contact Name'),
    'Contact Linkedin': contactLinkedIn,
    'Contact LinkedIn': contactLinkedIn,
    'Current Stage': 'Pending',
    'Current Progress': 'Connected',
    'Last Action Date': lastActionDate,
    'Next Action': schedule.nextAction,
    'Next Action Due': schedule.nextActionDue,
  };

  const existingRow = findRowByColumnValue_(sheet, pipelineIndex, 'Prospect ID', prospectId);
  if (existingRow) {
    headers.forEach((header, i) => {
      if (!(header in mapped)) {
        return;
      }
      if ((header === 'Next Action' || header === 'Next Action Due') && !mapped[header]) {
        return;
      }
      if (header === 'Current Stage') {
        const existingStage = String(sheet.getRange(existingRow, i + 1).getDisplayValue() || '').trim();
        if (existingStage) {
          return;
        }
      }
      if (header === 'Current Progress' || header === 'Last Action Date') {
        const existingValue = String(sheet.getRange(existingRow, i + 1).getDisplayValue() || '').trim();
        if (existingValue) {
          return;
        }
      }
      sheet.getRange(existingRow, i + 1).setValue(mapped[header]);
    });
    refreshPipelineNextActionForRow_(sheet, pipelineIndex, existingRow);
    return { ok: true, pipelineInserted: false };
  }

  const startRow = firstAppendRow_(sheet, headers.length);
  const payload = headers.map((header) => (mapped[header] !== undefined ? mapped[header] : ''));
  sheet.getRange(startRow, 1, 1, headers.length).setValues([payload]);
  return { ok: true, pipelineInserted: true };
}

function findRowByColumnValue_(sheet, index, column, value) {
  if (!(column in index)) {
    return 0;
  }
  const needle = String(value || '').trim();
  if (!needle) {
    return 0;
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return 0;
  }
  const values = sheet.getRange(2, index[column] + 1, lastRow - 1, 1).getDisplayValues();
  for (let i = 0; i < values.length; i++) {
    if (String(values[i][0] || '').trim() === needle) {
      return i + 2;
    }
  }
  return 0;
}

function getPipelinePayload(options) {
  const opts = options || {};
  const filter = String(opts.filter || 'active').trim().toLowerCase();
  const limit = Math.max(1, Math.min(Number(opts.limit || CONFIG.outreachLogPageSize), 1000));

  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return buildPipelinePayload_([], emptyPipelineCounts_(), filter);
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);
  const contactLinkedInHeader = contactLinkedInHeader_(index);
  if (!contactLinkedInHeader) {
    throw new Error('Missing required columns: Contact Linkedin');
  }
  const optionLists = {
    progressOptions: getValidationOptionsForColumn_(sheet, index['Current Progress'] + 1, PIPELINE_PROGRESS_VALUES),
    stageOptions: mergeOptions_(PIPELINE_STAGE_VALUES, getValidationOptionsForColumn_(sheet, index['Current Stage'] + 1, [])),
  };
  const prospectWebsiteById = buildProspectWebsiteById_();
  const messagingSheet = getMessagingSheet_();
  const messagingHeaders = messagingSheet.getRange(1, 1, 1, messagingSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const messagingRowsById = buildMessagingRowsById_(messagingSheet, buildHeaderIndex_(messagingHeaders));

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const counts = emptyPipelineCounts_();
  const entries = [];
  values.forEach((row, i) => {
    const prospectId = cell_(row, index, 'Prospect ID');
    const company = cell_(row, index, 'Company');
    if (!prospectId && !company) {
      return;
    }

    const progress = cell_(row, index, 'Current Progress');
    const stage = cell_(row, index, 'Current Stage');
    const dueInfo = buildPipelineDueInfo_(cell_(row, index, 'Next Action Due'), progress);
    const progressKey = classifyPipelineProgress_(progress);
    counts.all += 1;
    if (dueInfo.overdue) counts.overdue += 1;
    else if (dueInfo.dueToday) counts.due_today += 1;
    else if (dueInfo.value !== '') counts.upcoming += 1;
    if (progressKey === 'done') counts.done += 1;

    if (!pipelineMatchesFilter_(filter, dueInfo, progressKey)) {
      return;
    }
    if (entries.length >= limit) {
      return;
    }
    const messagingPushed = Boolean(messagingRowsById[prospectId]);
    entries.push({
      rowNumber: i + 2,
      prospectId,
      messagingPushed,
      company,
      website: prospectWebsiteById[prospectId] || '',
      personEngaged: cell_(row, index, 'Person Engaged'),
      contactName: cell_(row, index, 'Contact Name'),
      contactLinkedIn: normalizeUrl_(cell_(row, index, contactLinkedInHeader)),
      currentStage: stage,
      currentProgress: progress,
      progressKey,
      lastActionDate: normalizeDateDisplay_(cell_(row, index, 'Last Action Date')),
      nextAction: cell_(row, index, 'Next Action'),
      nextActionDue: normalizeDateDisplay_(cell_(row, index, 'Next Action Due')),
      dueLabel: dueInfo.display,
      dueValue: dueInfo.value,
      dueTone: dueInfo.tone,
    });
  });

  return buildPipelinePayload_(entries, counts, filter, optionLists);
}

function getMessagingPayload(options) {
  const entries = getConnectedMessagingEntries_();
  const drafted = entries.filter((entry) => entry.drafted).length;
  return buildMessagingPayload_(entries, {
    total: entries.length,
    drafted,
    pending: Math.max(0, entries.length - drafted),
  });
}

function getConnectedMessagingEntries_() {
  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return [];
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((value) => String(value).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);

  const contactLinkedInHeader = contactLinkedInHeader_(index);
  if (!contactLinkedInHeader) {
    throw new Error('Missing required columns: Contact Linkedin');
  }
  const prospectContactsById = buildProspectContactsById_();
  const draftsByProspect = buildMessageDraftsByProspect_('First Message');
  const entries = [];
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  values.forEach((row, i) => {
    const progress = normalizePipelineProgress_(cell_(row, index, 'Current Progress'));
    if (progress !== 'Connected') {
      return;
    }
    const prospectId = cell_(row, index, 'Prospect ID');
    if (!prospectId) {
      return;
    }
    const company = cell_(row, index, 'Company');
    const contactName = cell_(row, index, 'Contact Name');
    if (!company && !contactName) {
      return;
    }
    const personEngaged = cell_(row, index, 'Person Engaged');
    const contactLinkedin = normalizeUrl_(cell_(row, index, contactLinkedInHeader))
      || resolveContactLinkedInFromProspectLookup_(prospectContactsById, prospectId, personEngaged, contactName);
    const entry = {
      rowNumber: i + 2,
      prospectId,
      company,
      personEngaged,
      contactName,
      contactLinkedin,
      currentProgress: progress,
      lastActionDate: normalizeDateDisplay_(cell_(row, index, 'Last Action Date')),
      nextAction: 'First Message',
      firstName: firstNameFromContact_(contactName),
    };
    const draftRecord = draftsByProspect[prospectId] || ensureMessageTemplateAssignment_(entry);
    draftsByProspect[prospectId] = draftRecord;
    entry.drafted = Boolean(draftRecord && draftRecord.status === 'Drafted' && draftRecord.message);
    entry.draftTemplateId = draftRecord ? draftRecord.templateId : '';
    entry.draftedAt = draftRecord ? draftRecord.draftedAt : '';
    entry.draftMessage = draftRecord ? draftRecord.message : '';
    entries.push(entry);
  });

  return entries;
}

function draftMessagingMessage(prospectId, templateId, forceRedraft) {
  const id = String(prospectId || '').trim();
  if (!id) {
    throw new Error('Missing Prospect ID.');
  }
  const lead = findConnectedMessagingLead_(id);
  if (!lead) {
    throw new Error(`No connected Pipeline row found for Prospect ID: ${id}`);
  }

  const selectedTemplateId = String(templateId || '').trim();
  const shouldForceRedraft = Boolean(forceRedraft);
  const existing = findExistingMessageDraft_(id, 'First Message');
  if (!shouldForceRedraft && existing && existing.status === 'Drafted' && existing.message) {
    return {
      ok: true,
      reused: true,
      prospectId: id,
      templateId: existing.templateId,
      message: existing.message,
      draftedAt: existing.draftedAt,
      lead,
    };
  }

  const template = selectedTemplateId
    ? findMessagingTemplateById_(selectedTemplateId, 'First Message')
    : existing && existing.templateId
      ? findMessagingTemplateById_(existing.templateId, 'First Message')
      : nextMessageTemplate_('First Message');
  const message = renderMessageTemplate_(template.body, lead);
  const draftedAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
  const draftId = `${id}-${template.templateId}-${Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyyMMddHHmmss')}`;
  const sheet = getMessageDraftsSheet_();
  const headers = ensureMessageDraftHeaders_(sheet);
  const mapped = {
    'Draft ID': draftId,
    'Prospect ID': id,
    Stage: 'First Message',
    'Template ID': template.templateId,
    'Draft Message': message,
    'Drafted At': draftedAt,
    Status: 'Drafted',
  };
  if (existing && existing.rowNumber) {
    const index = buildHeaderIndex_(headers);
    headers.forEach((header) => {
      if (mapped[header] !== undefined) {
        sheet.getRange(existing.rowNumber, index[header] + 1).setValue(mapped[header]);
      }
    });
  } else {
    sheet.appendRow(headers.map((header) => (mapped[header] !== undefined ? mapped[header] : '')));
  }
  return {
    ok: true,
    reused: false,
    prospectId: id,
    templateId: template.templateId,
    message,
    draftedAt,
    lead,
  };
}

function regenerateMessagingQueue() {
  const entries = getConnectedMessagingEntries_();
  const templates = loadMessagingTemplates_('First Message');
  if (!templates.length) {
    throw new Error('No enabled first-message templates found.');
  }

  const sheet = getMessageDraftsSheet_();
  const headers = ensureMessageDraftHeaders_(sheet);
  const index = buildHeaderIndex_(headers);
  const existingByProspect = buildMessageDraftsByProspect_('First Message');
  const nowStamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyyMMddHHmmss');
  const assignedAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
  const appendRows = [];

  entries.forEach((entry) => {
    const template = templates[Math.floor(Math.random() * templates.length)];
    const mapped = {
      'Draft ID': `${entry.prospectId}-${template.templateId}-assigned-${nowStamp}`,
      'Prospect ID': entry.prospectId,
      Stage: 'First Message',
      'Template ID': template.templateId,
      'Draft Message': '',
      'Drafted At': '',
      Status: 'Assigned',
    };
    const existing = existingByProspect[entry.prospectId];
    if (existing && existing.rowNumber) {
      headers.forEach((header) => {
        if (mapped[header] !== undefined) {
          sheet.getRange(existing.rowNumber, index[header] + 1).setValue(mapped[header]);
        }
      });
    } else {
      appendRows.push(headers.map((header) => (mapped[header] !== undefined ? mapped[header] : '')));
    }
    entry.drafted = false;
    entry.draftTemplateId = template.templateId;
    entry.draftMessage = '';
    entry.draftedAt = '';
    entry.assignedAt = assignedAt;
  });

  if (appendRows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, appendRows.length, headers.length).setValues(appendRows);
  }

  return buildMessagingPayload_(entries, {
    total: entries.length,
    drafted: 0,
    pending: entries.length,
  });
}

function ensureMessageTemplateAssignment_(lead) {
  const prospectId = String(lead && lead.prospectId || '').trim();
  if (!prospectId) {
    return null;
  }
  const existing = findExistingMessageDraft_(prospectId, 'First Message');
  if (existing) {
    return existing;
  }
  const template = nextMessageTemplate_('First Message');
  const assignedAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
  const draftId = `${prospectId}-${template.templateId}-assigned-${Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyyMMddHHmmss')}`;
  const sheet = getMessageDraftsSheet_();
  const headers = ensureMessageDraftHeaders_(sheet);
  const mapped = {
    'Draft ID': draftId,
    'Prospect ID': prospectId,
    Stage: 'First Message',
    'Template ID': template.templateId,
    'Draft Message': '',
    'Drafted At': '',
    Status: 'Assigned',
  };
  sheet.appendRow(headers.map((header) => (mapped[header] !== undefined ? mapped[header] : '')));
  return {
    rowNumber: sheet.getLastRow(),
    templateId: template.templateId,
    message: '',
    draftedAt: '',
    status: 'Assigned',
    assignedAt,
  };
}

function buildMessageDraftsByProspect_(stage) {
  const sheet = getMessageDraftsSheet_();
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    return {};
  }
  const headers = values[0].map((value) => String(value).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, MESSAGE_DRAFT_REQUIRED_COLUMNS);
  const result = {};
  values.slice(1).forEach((row, i) => {
    const prospectId = cell_(row, index, 'Prospect ID');
    const rowStage = cell_(row, index, 'Stage');
    const status = cell_(row, index, 'Status');
    if (!prospectId || rowStage !== stage || status.toLowerCase() === 'discarded') {
      return;
    }
    result[prospectId] = {
      rowNumber: i + 2,
      templateId: cell_(row, index, 'Template ID'),
      message: cell_(row, index, 'Draft Message'),
      draftedAt: cell_(row, index, 'Drafted At'),
      status,
    };
  });
  return result;
}

function findExistingMessageDraft_(prospectId, stage) {
  return buildMessageDraftsByProspect_(stage)[prospectId] || null;
}

function findConnectedMessagingLead_(prospectId) {
  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return null;
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((value) => String(value).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);
  const contactLinkedInHeader = contactLinkedInHeader_(index);
  if (!contactLinkedInHeader) {
    throw new Error('Missing required columns: Contact Linkedin');
  }
  const prospectContactsById = buildProspectContactsById_();
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  for (let i = 0; i < values.length; i += 1) {
    const row = values[i];
    if (cell_(row, index, 'Prospect ID') !== prospectId) {
      continue;
    }
    if (normalizePipelineProgress_(cell_(row, index, 'Current Progress')) !== 'Connected') {
      continue;
    }
    const contactName = cell_(row, index, 'Contact Name');
    const personEngaged = cell_(row, index, 'Person Engaged');
    return {
      rowNumber: i + 2,
      prospectId,
      company: cell_(row, index, 'Company'),
      personEngaged,
      contactName,
      firstName: firstNameFromContact_(contactName),
      contactLinkedin: normalizeUrl_(cell_(row, index, contactLinkedInHeader))
        || resolveContactLinkedInFromProspectLookup_(prospectContactsById, prospectId, personEngaged, contactName),
      lastActionDate: normalizeDateDisplay_(cell_(row, index, 'Last Action Date')),
    };
  }
  return null;
}

function firstNameFromContact_(contactName) {
  const cleaned = String(contactName || '').replace(/\s+/g, ' ').trim();
  if (!cleaned) {
    return '';
  }
  return cleaned.split(' ')[0];
}

function nextMessageTemplate_(stage) {
  const templates = loadMessagingTemplates_(stage);
  if (!templates.length) {
    throw new Error(`No enabled messaging templates found for stage: ${stage}`);
  }
  const byId = {};
  templates.forEach((template) => {
    byId[template.templateId] = template;
  });

  const props = PropertiesService.getScriptProperties();
  const key = `messaging_template_queue_${stage.replace(/\s+/g, '_').toLowerCase()}`;
  let queue = [];
  try {
    queue = JSON.parse(props.getProperty(key) || '[]');
  } catch (error) {
    queue = [];
  }
  queue = queue.filter((templateId) => Boolean(byId[templateId]));
  if (!queue.length) {
    queue = shuffleArray_(templates.map((template) => template.templateId));
  }
  const nextId = queue.shift();
  props.setProperty(key, JSON.stringify(queue));
  return byId[nextId];
}

function loadMessagingTemplates_(stage) {
  const sheet = getMessagingTemplatesSheet_();
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    return [];
  }
  const headers = values[0].map((value) => String(value).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Template ID', 'Stage', 'Enabled', 'Message Body']);
  const targetStage = String(stage || '').trim();
  const templates = [];
  values.slice(1).forEach((row) => {
    const enabled = cell_(row, index, 'Enabled').toLowerCase();
    if (enabled && enabled !== 'yes' && enabled !== 'true' && enabled !== '1') {
      return;
    }
    if (cell_(row, index, 'Stage') !== targetStage) {
      return;
    }
    const templateId = cell_(row, index, 'Template ID');
    const body = cell_(row, index, 'Message Body');
    if (!templateId || !body) {
      return;
    }
    templates.push({
      templateId,
      stage: targetStage,
      body,
    });
  });
  return templates;
}

function findMessagingTemplateById_(templateId, stage) {
  const id = String(templateId || '').trim();
  const template = loadMessagingTemplates_(stage).find((item) => item.templateId === id);
  if (!template) {
    throw new Error(`Messaging template not found or disabled: ${id}`);
  }
  return template;
}

function renderMessageTemplate_(body, lead) {
  const firstName = lead.firstName || firstNameFromContact_(lead.contactName) || '';
  const company = lead.company || '';
  const data = {
    firstname: firstName,
    first: firstName,
    contactname: lead.contactName || '',
    name: lead.contactName || firstName,
    company,
    companyname: company,
    firm: company,
    firmname: company,
  };
  const normalizedBody = String(body || '').replace(/\\n/g, '\n');
  return normalizedBody.replace(/([\[{])\s*([^\]\}]+?)\s*([\]}])/g, (match, open, rawKey, close) => {
    if ((open === '[' && close !== ']') || (open === '{' && close !== '}')) {
      return match;
    }
    const normalizedKey = String(rawKey || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    return Object.prototype.hasOwnProperty.call(data, normalizedKey) ? data[normalizedKey] : match;
  });
}

function escapeRegExp_(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function shuffleArray_(values) {
  const items = values.slice();
  for (let i = items.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const temp = items[i];
    items[i] = items[j];
    items[j] = temp;
  }
  return items;
}

function ensureMessageDraftHeaders_(sheet) {
  const headers = sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), MESSAGE_DRAFT_REQUIRED_COLUMNS.length))
    .getDisplayValues()[0]
    .map((header) => String(header).trim());
  let needsUpdate = false;
  MESSAGE_DRAFT_REQUIRED_COLUMNS.forEach((header, i) => {
    if (headers[i] !== header) {
      needsUpdate = true;
    }
  });
  if (needsUpdate) {
    sheet.getRange(1, 1, 1, MESSAGE_DRAFT_REQUIRED_COLUMNS.length).setValues([MESSAGE_DRAFT_REQUIRED_COLUMNS]);
  }
  return MESSAGE_DRAFT_REQUIRED_COLUMNS.slice();
}

function updatePipeline(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return { ok: true, processed: 0, updatedAt: new Date().toISOString() };
  }

  const sheet = getPipelineSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);

  const lastRow = sheet.getLastRow();
  rows.forEach((item) => {
    const rowNumber = Number(item && item.rowNumber);
    if (!Number.isInteger(rowNumber) || rowNumber < 2 || rowNumber > lastRow) {
      throw new Error(`Invalid Pipeline row number: ${item && item.rowNumber}`);
    }
    const existingRowValues = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
    const oldProgress = cell_(existingRowValues, index, 'Current Progress');
    const oldStage = cell_(existingRowValues, index, 'Current Stage');

    if (Object.prototype.hasOwnProperty.call(item, 'currentProgress')) {
      const nextProgress = String(item.currentProgress || '').trim();
      sheet.getRange(rowNumber, index['Current Progress'] + 1).setValue(nextProgress);
      trackPipelineProgressMetric_(oldProgress, nextProgress);
      sheet.getRange(rowNumber, index['Last Action Date'] + 1)
        .setValue(formatDateOnly_(new Date()));
    }
    if (Object.prototype.hasOwnProperty.call(item, 'currentStage')) {
      const nextStage = String(item.currentStage || '').trim();
      sheet.getRange(rowNumber, index['Current Stage'] + 1).setValue(nextStage);
      trackPipelineStageMetric_(oldStage, nextStage);
    }
    if (Object.prototype.hasOwnProperty.call(item, 'lastActionDate')) {
      const lastActionDate = normalizeDateDisplay_(item.lastActionDate);
      sheet.getRange(rowNumber, index['Last Action Date'] + 1)
        .setValue(lastActionDate);
    }
    refreshPipelineNextActionForRow_(sheet, index, rowNumber);
  });

  return {
    ok: true,
    processed: rows.length,
    updatedAt: new Date().toISOString(),
  };
}

function emptyPipelineCounts_() {
  return { all: 0, overdue: 0, due_today: 0, upcoming: 0, done: 0 };
}

function buildPipelinePayload_(entries, counts, filter, optionLists) {
  const options = optionLists || {};
  const unpushedCount = entries.filter((entry) => !entry.messagingPushed).length;
  return {
    ok: true,
    pipelineTabName: CONFIG.pipelineTabName,
    entries,
    counts,
    unpushedCount,
    filter: filter || 'active',
    progressOptions: options.progressOptions || PIPELINE_PROGRESS_VALUES,
    stageOptions: options.stageOptions || PIPELINE_STAGE_VALUES,
    generatedAt: new Date().toISOString(),
  };
}

function buildMessagingPayload_(entries, counts) {
  const summary = counts || {};
  const total = summary.total !== undefined ? summary.total : entries.length;
  const drafted = summary.drafted || 0;
  const templates = loadMessagingTemplates_('First Message').map((template) => ({
    templateId: template.templateId,
    stage: template.stage,
  }));
  return {
    ok: true,
    messagingTabName: CONFIG.messagingTabName,
    entries,
    templates,
    counts: {
      total,
      drafted,
      pending: summary.pending !== undefined ? summary.pending : Math.max(0, total - drafted),
    },
    generatedAt: new Date().toISOString(),
  };
}

function getDeployedDocTabHtml_(tabId, tabTitle) {
  try {
    const webhookUrl = getRequiredConfig_('docWebhookUrl');
    const webhookSecret = getRequiredConfig_('docWebhookSecret');
    const params = {
      action: 'getTab',
      secret: webhookSecret,
    };
    if (tabId) {
      params.tabId = tabId;
    } else {
      params.tabTitle = tabTitle;
    }
    const query = Object.keys(params)
      .map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(params[key])}`)
      .join('&');
    const response = UrlFetchApp.fetch(`${webhookUrl}?${query}`, {
      method: 'get',
      muteHttpExceptions: true,
    });
    const status = response.getResponseCode();
    const text = response.getContentText() || '';
    const payload = JSON.parse(text || '{}');
    if (!payload.ok) {
      return {
        html: '',
        status,
        source: 'doc_endpoint_error',
        error: payload.error || `Doc endpoint returned ok=false (${status})`,
      };
    }
    const html = payload.contentHtml || payload.html || '';
    return {
      html,
      status,
      source: html ? 'doc_endpoint_html' : 'doc_endpoint_empty',
      error: html ? '' : 'Doc endpoint returned ok=true without contentHtml.',
    };
  } catch (error) {
    return {
      html: '',
      status: '',
      source: 'doc_endpoint_exception',
      error: error && error.message ? error.message : String(error),
    };
  }
}

function getDocumentTabHtml_(documentId, tabId) {
  try {
    const doc = DocumentApp.openById(documentId);
    const body = getDocumentBodyForTab_(doc, tabId);
    return body ? documentBodyToHtml_(body) : '';
  } catch (error) {
    return '';
  }
}

function getDocumentBodyForTab_(doc, tabId) {
  if (tabId && typeof doc.getTab === 'function') {
    const tab = doc.getTab(tabId);
    if (tab && typeof tab.asDocumentTab === 'function') {
      return tab.asDocumentTab().getBody();
    }
  }
  return doc.getBody();
}

function documentBodyToHtml_(body) {
  const parts = [];
  for (let i = 0; i < body.getNumChildren(); i += 1) {
    const child = body.getChild(i);
    const type = child.getType();
    if (type === DocumentApp.ElementType.PARAGRAPH) {
      const paragraph = child.asParagraph();
      const text = paragraph.getText();
      if (!text) {
        continue;
      }
      const tag = paragraphTag_(paragraph.getHeading());
      parts.push(`<${tag}>${textElementToHtml_(paragraph)}</${tag}>`);
    } else if (type === DocumentApp.ElementType.LIST_ITEM) {
      const items = [];
      let cursor = i;
      while (cursor < body.getNumChildren() && body.getChild(cursor).getType() === DocumentApp.ElementType.LIST_ITEM) {
        items.push(`<li>${textElementToHtml_(body.getChild(cursor).asListItem())}</li>`);
        cursor += 1;
      }
      parts.push(`<ul>${items.join('')}</ul>`);
      i = cursor - 1;
    } else if (type === DocumentApp.ElementType.TABLE) {
      parts.push(tableToHtml_(child.asTable()));
    }
  }
  return parts.join('');
}

function paragraphTag_(heading) {
  if (heading === DocumentApp.ParagraphHeading.HEADING1 || heading === DocumentApp.ParagraphHeading.TITLE) return 'h1';
  if (heading === DocumentApp.ParagraphHeading.HEADING2 || heading === DocumentApp.ParagraphHeading.SUBTITLE) return 'h2';
  if (heading === DocumentApp.ParagraphHeading.HEADING3) return 'h3';
  return 'p';
}

function textElementToHtml_(element) {
  const text = element.editAsText();
  const value = text.getText();
  if (!value) {
    return '';
  }
  const indices = text.getTextAttributeIndices();
  const runs = [];
  for (let i = 0; i < indices.length; i += 1) {
    const start = indices[i];
    const end = (i + 1 < indices.length ? indices[i + 1] : value.length) - 1;
    let fragment = escapeHtml_(value.slice(start, end + 1));
    const url = text.getLinkUrl(start);
    if (text.isBold(start)) fragment = `<strong>${fragment}</strong>`;
    if (text.isItalic(start)) fragment = `<em>${fragment}</em>`;
    if (text.isUnderline(start)) fragment = `<u>${fragment}</u>`;
    if (url) fragment = `<a href="${escapeAttr_(url)}" target="_blank" rel="noopener">${fragment}</a>`;
    runs.push(fragment);
  }
  return runs.join('');
}

function tableToHtml_(table) {
  const rows = [];
  for (let r = 0; r < table.getNumRows(); r += 1) {
    const row = table.getRow(r);
    const cells = [];
    for (let c = 0; c < row.getNumCells(); c += 1) {
      cells.push(`<td>${escapeHtml_(row.getCell(c).getText())}</td>`);
    }
    rows.push(`<tr>${cells.join('')}</tr>`);
  }
  return `<table>${rows.join('')}</table>`;
}

function documentIdFromUrl_(url) {
  const match = String(url || '').match(/\/document\/d\/([^/]+)/);
  return match ? match[1] : '';
}

function escapeHtml_(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeAttr_(value) {
  return escapeHtml_(value);
}

function pipelineMatchesFilter_(filter, dueInfo, progressKey) {
  if (filter === 'all') return true;
  if (filter === 'overdue') return Boolean(dueInfo.overdue);
  if (filter === 'due_today') return Boolean(dueInfo.dueToday);
  if (filter === 'done') return progressKey === 'done';
  return progressKey !== 'done';
}

function buildPipelineDueInfo_(dueDisplay, progress) {
  if (classifyPipelineProgress_(progress) === 'done') {
    return {
      display: 'Done',
      value: '',
      overdue: false,
      dueToday: false,
      tone: 'neutral',
    };
  }
  const dueDate = parseSheetDate_(dueDisplay);
  if (!dueDate) {
    return {
      display: '',
      value: '',
      overdue: false,
      dueToday: false,
      tone: 'blank',
    };
  }
  const today = copyDateOnly_(new Date());
  const diff = Math.floor((copyDateOnly_(dueDate).getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
  const overdue = diff < 0;
  const dueToday = diff === 0;
  let tone = 'green';
  if (overdue) tone = 'red';
  else if (dueToday || diff <= 2) tone = 'amber';
  return {
    display: overdue
      ? `${Math.abs(diff)} day${Math.abs(diff) === 1 ? '' : 's'} overdue`
      : dueToday
        ? 'Due today'
        : `${diff} day${diff === 1 ? '' : 's'} left`,
    value: diff,
    overdue,
    dueToday,
    tone,
  };
}

function classifyPipelineProgress_(progress) {
  const normalized = normalizePipelineProgress_(progress);
  if (!normalized) return 'blank';
  if (normalized === 'Fail' || normalized === 'FU-10') return 'done';
  if (normalized === 'Connected') return 'connected';
  if (normalized === 'First Message') return 'first_message';
  if (/^FU-\d+$/.test(normalized)) return 'follow_up';
  return 'other';
}

function normalizeDateDisplay_(value) {
  const date = parseSheetDate_(value);
  return date ? formatDateOnly_(date) : String(value || '').trim();
}

function trackPipelineProgressMetric_(oldProgress, nextProgress) {
  const oldValue = normalizePipelineProgress_(oldProgress);
  const nextValue = normalizePipelineProgress_(nextProgress);
  if (!nextValue || oldValue === nextValue) {
    return;
  }
  if (nextValue === 'First Message') {
    incrementDailyMetrics_({ 'First Msgs Sent': 1 });
  } else if (/^FU-\d+$/.test(nextValue)) {
    incrementDailyMetrics_({ 'Follow-Ups Sent': 1 });
  }
}

function trackPipelineStageMetric_(oldStage, nextStage) {
  const oldValue = String(oldStage || '').trim();
  const nextValue = String(nextStage || '').trim();
  if (!nextValue || oldValue === nextValue) {
    return;
  }
  const increments = {};
  if (nextValue === 'Call Scheduled') {
    increments['Calls Booked'] = 1;
  } else if (nextValue === 'Audit Sent') {
    increments['Audits Sent'] = 1;
  } else if (nextValue === 'Proposal Sent') {
    increments['Proposals Sent'] = 1;
  }
  if (Object.keys(increments).length) {
    incrementDailyMetrics_(increments);
  }
}

function incrementDailyMetrics_(increments) {
  const patch = increments || {};
  const columns = Object.keys(patch).filter((column) => Number(patch[column] || 0) !== 0);
  if (!columns.length) {
    return { ok: true, updated: false };
  }

  const sheet = getDailyMetricsSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, DAILY_METRICS_REQUIRED_COLUMNS);

  const today = metricDateKeyFromDate_(new Date());
  const rowNumber = getOrCreateDailyMetricsRow_(sheet, index, headers.length, today);
  columns.forEach((column) => {
    if (!(column in index)) {
      throw new Error(`Missing required columns: ${column}`);
    }
    const cell = sheet.getRange(rowNumber, index[column] + 1);
    const nextValue = numericCellValue_(cell.getDisplayValue()) + Number(patch[column] || 0);
    cell.setValue(nextValue);
  });
  refreshDailyMetricsFormulas_(sheet, index, rowNumber);

  return {
    ok: true,
    updated: true,
    rowNumber,
    date: today,
    increments: patch,
  };
}

function refreshDailyMetrics() {
  const sheet = getDailyMetricsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {
      ok: true,
      refreshed: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, DAILY_METRICS_REQUIRED_COLUMNS);

  let refreshed = 0;
  for (let rowNumber = 2; rowNumber <= lastRow; rowNumber++) {
    const row = sheet.getRange(rowNumber, 1, 1, lastColumn).getDisplayValues()[0];
    if (!cell_(row, index, 'Date')) {
      continue;
    }
    refreshDailyMetricsFormulas_(sheet, index, rowNumber);
    refreshed += 1;
  }

  return {
    ok: true,
    refreshed,
    updatedAt: new Date().toISOString(),
  };
}

function backfillDailyMetricsFromExistingLogs() {
  const metrics = {};
  const outreachStats = collectOutreachLogMetrics_(metrics);
  const pipelineStats = collectPipelineMetrics_(metrics);
  const autoColumns = [
    'Conn Sent',
    'Accepted',
    'First Msgs Sent',
    'Follow-Ups Sent',
    'Requests Withdrawn',
    'New Pipeline',
    'Calls Booked',
    'Audits Sent',
    'Proposals Sent',
  ];

  const sheet = getDailyMetricsSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, DAILY_METRICS_REQUIRED_COLUMNS);

  const existingRowsCleared = clearDailyMetricsAutoColumns_(sheet, index, autoColumns);
  const dateKeys = Object.keys(metrics).sort();
  dateKeys.forEach((dateKey) => {
    const rowNumber = getOrCreateDailyMetricsRow_(sheet, index, headers.length, dateKey);
    autoColumns.forEach((column) => {
      sheet.getRange(rowNumber, index[column] + 1).setValue(metrics[dateKey][column] || 0);
    });
  });

  sortDailyMetricsByDate_(sheet, headers, index);
  const weekendRowsRemoved = removeEmptyWeekendDailyMetricRows_(sheet, headers, index);
  const refreshResult = refreshDailyMetrics();

  return {
    ok: true,
    datesUpdated: dateKeys.length,
    existingRowsCleared,
    weekendRowsRemoved,
    outreachRowsScanned: outreachStats.rowsScanned,
    pipelineRowsScanned: pipelineStats.rowsScanned,
    counts: dateKeys.reduce((acc, dateKey) => {
      acc[dateKey] = metrics[dateKey];
      return acc;
    }, {}),
    formulasRefreshed: refreshResult.refreshed,
    updatedAt: new Date().toISOString(),
  };
}

function getMetricsPayload(options) {
  const opts = options || {};
  const limit = Math.max(1, Math.min(Number(opts.limit || 14), 60));
  const weeklyLimit = Math.max(1, Math.min(Number(opts.weeklyLimit || 12), 60));
  const sheet = getDailyMetricsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return buildMetricsPayload_([], {
      weeklyEntries: [],
      weeklyLatest: {},
    });
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, DAILY_METRICS_REQUIRED_COLUMNS);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const rows = [];
  values.forEach((row, i) => {
    const date = cell_(row, index, 'Date');
    if (!date) {
      return;
    }
    rows.push(dailyMetricFromRow_(row, index, i + 2));
  });
  rows.sort((a, b) => {
    return String(b.dateKey || b.date).localeCompare(String(a.dateKey || a.date));
  });

  const entries = rows.slice(0, limit);
  const latest = entries[0] || {};
  const weeklyRows = buildWeeklyMetricRows_(rows);
  const weeklyEntries = weeklyRows.slice(0, weeklyLimit);
  return buildMetricsPayload_(entries, {
    latest,
    totalRows: rows.length,
    weeklyEntries,
    weeklyLatest: weeklyEntries[0] || {},
    weeklyTotalRows: weeklyRows.length,
  });
}

function rebuildDailyMetricsReport() {
  const rebuild = backfillDailyMetricsFromExistingLogs();
  const payload = getMetricsPayload();
  payload.rebuild = rebuild;
  return payload;
}

function updateDailyMetricsManual(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return { ok: true, processed: 0, updatedAt: new Date().toISOString() };
  }

  const editableColumnsByField = {
    connTarget: 'Conn Target',
    replies: 'Replies',
    commentsPosted: 'Comments Posted',
  };
  const sheet = getDailyMetricsSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, DAILY_METRICS_REQUIRED_COLUMNS);

  const lastRow = sheet.getLastRow();
  rows.forEach((item) => {
    const rowNumber = Number(item && item.rowNumber);
    if (!Number.isInteger(rowNumber) || rowNumber < 2 || rowNumber > lastRow) {
      throw new Error(`Invalid Daily Metrics row number: ${item && item.rowNumber}`);
    }
    Object.keys(editableColumnsByField).forEach((field) => {
      if (!Object.prototype.hasOwnProperty.call(item, field)) {
        return;
      }
      const column = editableColumnsByField[field];
      const rawValue = String(item[field] || '').trim();
      const nextValue = rawValue === '' ? '' : numericCellValue_(rawValue);
      sheet.getRange(rowNumber, index[column] + 1).setValue(nextValue);
    });
    refreshDailyMetricsFormulas_(sheet, index, rowNumber);
  });

  return {
    ok: true,
    processed: rows.length,
    updatedAt: new Date().toISOString(),
  };
}

function dailyMetricFromRow_(row, index, rowNumber) {
  const date = cell_(row, index, 'Date');
  return {
    rowNumber,
    date,
    dateKey: dateKeyFromCell_(date),
    connSent: cell_(row, index, 'Conn Sent'),
    connTarget: cell_(row, index, 'Conn Target') || '30',
    accepted: cell_(row, index, 'Accepted'),
    totalConnSent: cell_(row, index, 'Total Conn Sent'),
    totalAccepted: cell_(row, index, 'Total Accepted'),
    acceptanceRate: cell_(row, index, 'All-Time Acceptance Rate'),
    firstMsgsSent: cell_(row, index, 'First Msgs Sent'),
    followUpsSent: cell_(row, index, 'Follow-Ups Sent'),
    replies: cell_(row, index, 'Replies'),
    replyRate: cell_(row, index, 'Reply Rate'),
    commentsPosted: cell_(row, index, 'Comments Posted'),
    requestsWithdrawn: cell_(row, index, 'Requests Withdrawn'),
    newPipeline: cell_(row, index, 'New Pipeline'),
    callsBooked: cell_(row, index, 'Calls Booked'),
    auditsSent: cell_(row, index, 'Audits Sent'),
    proposalsSent: cell_(row, index, 'Proposals Sent'),
    notes: cell_(row, index, 'Notes'),
  };
}

function buildWeeklyMetricRows_(dailyRows) {
  const groups = {};
  (dailyRows || []).forEach((row) => {
    const date = parseSheetDate_(row.dateKey || row.date);
    if (!date) {
      return;
    }
    const weekStart = startOfWeekMonday_(date);
    const weekKey = formatDateOnly_(weekStart);
    if (!groups[weekKey]) {
      groups[weekKey] = {
        dateKey: weekKey,
        weekNumber: isoWeekNumber_(weekStart),
        weekStarting: weekKey,
        weekEnding: formatDateOnly_(addCalendarDays_(weekStart, 6)),
        connSent: 0,
        connTarget: 0,
        accepted: 0,
        firstMsgsSent: 0,
        followUpsSent: 0,
        replies: 0,
        commentsPosted: 0,
        requestsWithdrawn: 0,
        newPipeline: 0,
        callsBooked: 0,
        auditsSent: 0,
        proposalsSent: 0,
      };
    }
    const group = groups[weekKey];
    group.connSent += numericCellValue_(row.connSent);
    group.connTarget += numericCellValue_(row.connTarget);
    group.accepted += numericCellValue_(row.accepted);
    group.firstMsgsSent += numericCellValue_(row.firstMsgsSent);
    group.followUpsSent += numericCellValue_(row.followUpsSent);
    group.replies += numericCellValue_(row.replies);
    group.commentsPosted += numericCellValue_(row.commentsPosted);
    group.requestsWithdrawn += numericCellValue_(row.requestsWithdrawn);
    group.newPipeline += numericCellValue_(row.newPipeline);
    group.callsBooked += numericCellValue_(row.callsBooked);
    group.auditsSent += numericCellValue_(row.auditsSent);
    group.proposalsSent += numericCellValue_(row.proposalsSent);
  });

  return Object.keys(groups).sort().reverse().map((weekKey) => {
    const group = groups[weekKey];
    const messageCount = group.firstMsgsSent + group.followUpsSent;
    group.connVariance = group.connSent - group.connTarget;
    group.acceptanceRate = formatPercent_(group.connSent ? group.accepted / group.connSent : 0);
    group.replyRate = formatPercent_(messageCount ? group.replies / messageCount : 0);
    WEEKLY_METRIC_SUM_COLUMNS.forEach((column) => {
      const field = metricFieldForColumn_(column);
      group[field] = String(group[field] || 0);
    });
    group.connVariance = String(group.connVariance || 0);
    return group;
  });
}

function metricFieldForColumn_(column) {
  const fields = {
    'Conn Sent': 'connSent',
    'Conn Target': 'connTarget',
    Accepted: 'accepted',
    'First Msgs Sent': 'firstMsgsSent',
    'Follow-Ups Sent': 'followUpsSent',
    Replies: 'replies',
    'Comments Posted': 'commentsPosted',
    'Requests Withdrawn': 'requestsWithdrawn',
    'New Pipeline': 'newPipeline',
    'Calls Booked': 'callsBooked',
    'Audits Sent': 'auditsSent',
    'Proposals Sent': 'proposalsSent',
  };
  return fields[column] || '';
}

function startOfWeekMonday_(date) {
  const start = copyDateOnly_(date);
  const day = start.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  start.setDate(start.getDate() + diff);
  return start;
}

function isoWeekNumber_(date) {
  const target = copyDateOnly_(date);
  target.setDate(target.getDate() + 3 - ((target.getDay() + 6) % 7));
  const firstThursday = new Date(target.getFullYear(), 0, 4);
  return 1 + Math.round(((target.getTime() - firstThursday.getTime()) / 86400000
    - 3 + ((firstThursday.getDay() + 6) % 7)) / 7);
}

function formatPercent_(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function buildMetricsPayload_(entries, meta) {
  const payloadMeta = meta || {};
  return {
    ok: true,
    dailyMetricsTabName: CONFIG.dailyMetricsTabName,
    entries,
    weeklyEntries: payloadMeta.weeklyEntries || [],
    latest: payloadMeta.latest || entries[0] || {},
    weeklyLatest: payloadMeta.weeklyLatest || (payloadMeta.weeklyEntries || [])[0] || {},
    totalRows: payloadMeta.totalRows || entries.length,
    weeklyTotalRows: payloadMeta.weeklyTotalRows || (payloadMeta.weeklyEntries || []).length,
    generatedAt: new Date().toISOString(),
  };
}

function clearDailyMetricsAutoColumns_(sheet, index, columns) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return 0;
  }
  const lastColumn = sheet.getLastColumn();
  const rows = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  let cleared = 0;
  rows.forEach((row, i) => {
    if (!cell_(row, index, 'Date')) {
      return;
    }
    const rowNumber = i + 2;
    columns.forEach((column) => {
      sheet.getRange(rowNumber, index[column] + 1).setValue(0);
    });
    cleared += 1;
  });
  return cleared;
}

function collectOutreachLogMetrics_(metrics) {
  const sheet = getOutreachLogSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return { rowsScanned: 0 };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Prospect ID', 'Current Progress', 'Last Action Date', 'Sent At']);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const sentByProspect = {};
  const acceptedByProspect = {};
  const withdrawnByProspect = {};
  let rowsScanned = 0;

  values.forEach((row, i) => {
    const prospectId = cell_(row, index, 'Prospect ID') || `outreach-row-${i + 2}`;
    const progressKey = classifyProgress_(cell_(row, index, 'Current Progress'));
    const sentAt = metricDateKeyFromCell_(cell_(row, index, 'Sent At'));
    const lastActionDate = metricDateKeyFromCell_(cell_(row, index, 'Last Action Date'));
    if (!cell_(row, index, 'Prospect ID') && !sentAt && !lastActionDate) {
      return;
    }
    rowsScanned += 1;

    if (sentAt) {
      sentByProspect[prospectId] = earliestDateKey_(sentByProspect[prospectId], sentAt);
    }
    if (progressKey === 'connected' && lastActionDate) {
      acceptedByProspect[prospectId] = earliestDateKey_(acceptedByProspect[prospectId], lastActionDate);
    }
    if (progressKey === 'withdrawn' && lastActionDate) {
      withdrawnByProspect[prospectId] = earliestDateKey_(withdrawnByProspect[prospectId], lastActionDate);
    }
  });

  addMetricCountsByDate_(metrics, sentByProspect, 'Conn Sent');
  addMetricCountsByDate_(metrics, acceptedByProspect, 'Accepted');
  addMetricCountsByDate_(metrics, withdrawnByProspect, 'Requests Withdrawn');

  return { rowsScanned };
}

function collectPipelineMetrics_(metrics) {
  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return { rowsScanned: 0 };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const newPipelineByProspect = {};
  const firstMessageByProspect = {};
  const followUpByProspect = {};
  const callsBookedByProspect = {};
  const auditsSentByProspect = {};
  const proposalsSentByProspect = {};
  let rowsScanned = 0;

  values.forEach((row, i) => {
    const rawProspectId = cell_(row, index, 'Prospect ID');
    const prospectId = rawProspectId || `pipeline-row-${i + 2}`;
    const company = cell_(row, index, 'Company');
    const lastActionDate = metricDateKeyFromCell_(cell_(row, index, 'Last Action Date'));
    if (!rawProspectId && !company && !lastActionDate) {
      return;
    }
    rowsScanned += 1;
    if (!lastActionDate) {
      return;
    }

    newPipelineByProspect[prospectId] = earliestDateKey_(newPipelineByProspect[prospectId], lastActionDate);

    const progress = normalizePipelineProgress_(cell_(row, index, 'Current Progress'));
    if (progress === 'First Message') {
      firstMessageByProspect[prospectId] = earliestDateKey_(firstMessageByProspect[prospectId], lastActionDate);
    } else if (/^FU-\d+$/.test(progress)) {
      followUpByProspect[`${prospectId}:${progress}`] = earliestDateKey_(followUpByProspect[`${prospectId}:${progress}`], lastActionDate);
    }

    const stage = cell_(row, index, 'Current Stage');
    if (stage === 'Call Scheduled') {
      callsBookedByProspect[prospectId] = earliestDateKey_(callsBookedByProspect[prospectId], lastActionDate);
    } else if (stage === 'Audit Sent') {
      auditsSentByProspect[prospectId] = earliestDateKey_(auditsSentByProspect[prospectId], lastActionDate);
    } else if (stage === 'Proposal Sent') {
      proposalsSentByProspect[prospectId] = earliestDateKey_(proposalsSentByProspect[prospectId], lastActionDate);
    }
  });

  addMetricCountsByDate_(metrics, newPipelineByProspect, 'New Pipeline');
  addMetricCountsByDate_(metrics, firstMessageByProspect, 'First Msgs Sent');
  addMetricCountsByDate_(metrics, followUpByProspect, 'Follow-Ups Sent');
  addMetricCountsByDate_(metrics, callsBookedByProspect, 'Calls Booked');
  addMetricCountsByDate_(metrics, auditsSentByProspect, 'Audits Sent');
  addMetricCountsByDate_(metrics, proposalsSentByProspect, 'Proposals Sent');

  return { rowsScanned };
}

function addMetricCountsByDate_(metrics, dateByEntity, column) {
  Object.keys(dateByEntity).forEach((key) => {
    const dateKey = dateByEntity[key];
    if (!dateKey) {
      return;
    }
    if (!metrics[dateKey]) {
      metrics[dateKey] = {};
    }
    metrics[dateKey][column] = Number(metrics[dateKey][column] || 0) + 1;
  });
}

function earliestDateKey_(current, candidate) {
  if (!current) {
    return candidate;
  }
  return candidate < current ? candidate : current;
}

function dateKeyFromCell_(value) {
  const date = parseSheetDate_(value);
  return date ? formatDateOnly_(date) : '';
}

function metricDateKeyFromCell_(value) {
  const date = parseSheetDate_(value);
  return date ? metricDateKeyFromDate_(date) : '';
}

function metricDateKeyFromDate_(date) {
  return formatDateOnly_(nextBusinessDate_(date));
}

function nextBusinessDate_(date) {
  const next = copyDateOnly_(date);
  const day = next.getDay();
  if (day === 6) {
    next.setDate(next.getDate() + 2);
  } else if (day === 0) {
    next.setDate(next.getDate() + 1);
  }
  return next;
}

function isWeekendDateKey_(dateKey) {
  const date = parseSheetDate_(dateKey);
  if (!date) {
    return false;
  }
  const day = date.getDay();
  return day === 0 || day === 6;
}

function removeEmptyWeekendDailyMetricRows_(sheet, headers, index) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return 0;
  }
  const manualColumns = ['Conn Target', 'Replies', 'Comments Posted', 'Notes'];
  let removed = 0;
  for (let rowNumber = lastRow; rowNumber >= 2; rowNumber--) {
    const row = sheet.getRange(rowNumber, 1, 1, headers.length).getDisplayValues()[0];
    const dateKey = dateKeyFromCell_(cell_(row, index, 'Date'));
    if (!dateKey || !isWeekendDateKey_(dateKey)) {
      continue;
    }
    const hasManualValue = manualColumns.some((column) => {
      return column in index && cell_(row, index, column);
    });
    if (hasManualValue) {
      continue;
    }
    sheet.deleteRow(rowNumber);
    removed += 1;
  }
  return removed;
}

function sortDailyMetricsByDate_(sheet, headers, index) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 3) {
    return;
  }
  const values = sheet.getRange(2, 1, lastRow - 1, headers.length).getDisplayValues();
  const rows = values.filter((row) => row.some((value) => String(value || '').trim()));
  rows.sort((a, b) => {
    const dateA = dateKeyFromCell_(a[index.Date]);
    const dateB = dateKeyFromCell_(b[index.Date]);
    if (!dateA && !dateB) return 0;
    if (!dateA) return 1;
    if (!dateB) return -1;
    return dateA.localeCompare(dateB);
  });
  sheet.getRange(2, 1, lastRow - 1, headers.length).clearContent();
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
  }
}

function getOrCreateDailyMetricsRow_(sheet, index, columnCount, dateKey) {
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const values = sheet.getRange(2, index.Date + 1, lastRow - 1, 1).getDisplayValues();
    for (let i = 0; i < values.length; i++) {
      if (normalizeDateDisplay_(values[i][0]) === dateKey) {
        return i + 2;
      }
    }
  }

  const rowNumber = Math.max(2, firstAppendRow_(sheet, columnCount));
  sheet.getRange(rowNumber, index.Date + 1).setValue(dateKey);
  if ('Conn Target' in index) {
    sheet.getRange(rowNumber, index['Conn Target'] + 1).setValue(30);
  }
  refreshDailyMetricsFormulas_(sheet, index, rowNumber);
  return rowNumber;
}

function refreshDailyMetricsFormulas_(sheet, index, rowNumber) {
  const row = Number(rowNumber);
  const connSentCol = columnLetter_(index['Conn Sent'] + 1);
  const acceptedCol = columnLetter_(index.Accepted + 1);
  const totalConnSentCol = columnLetter_(index['Total Conn Sent'] + 1);
  const totalAcceptedCol = columnLetter_(index['Total Accepted'] + 1);
  const firstMsgsCol = columnLetter_(index['First Msgs Sent'] + 1);
  const followUpsCol = columnLetter_(index['Follow-Ups Sent'] + 1);
  const repliesCol = columnLetter_(index.Replies + 1);

  sheet.getRange(row, index['Total Conn Sent'] + 1)
    .setFormula(`=IFERROR(SUM(${connSentCol}$2:${connSentCol}${row}),0)`);
  sheet.getRange(row, index['Total Accepted'] + 1)
    .setFormula(`=IFERROR(SUM(${acceptedCol}$2:${acceptedCol}${row}),0)`);
  sheet.getRange(row, index['All-Time Acceptance Rate'] + 1)
    .setFormula(`=IFERROR(${totalAcceptedCol}${row}/${totalConnSentCol}${row},0)`)
    .setNumberFormat('0.0%');
  sheet.getRange(row, index['Reply Rate'] + 1)
    .setFormula(`=IFERROR(${repliesCol}${row}/(${firstMsgsCol}${row}+${followUpsCol}${row}),0)`)
    .setNumberFormat('0.0%');
}

function numericCellValue_(value) {
  const raw = String(value || '').trim();
  if (!raw) {
    return 0;
  }
  const cleaned = raw.replace(/,/g, '').replace(/%$/, '');
  const number = Number(cleaned);
  return isNaN(number) ? 0 : number;
}

function columnLetter_(columnNumber) {
  let column = Number(columnNumber);
  let letter = '';
  while (column > 0) {
    const remainder = (column - 1) % 26;
    letter = String.fromCharCode(65 + remainder) + letter;
    column = Math.floor((column - 1) / 26);
  }
  return letter;
}

function backfillPipelineNextActions() {
  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {
      ok: true,
      scanned: 0,
      updated: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);

  let scanned = 0;
  let updated = 0;
  for (let rowNumber = 2; rowNumber <= lastRow; rowNumber++) {
    const row = sheet.getRange(rowNumber, 1, 1, lastColumn).getDisplayValues()[0];
    if (!cell_(row, index, 'Prospect ID') && !cell_(row, index, 'Company')) {
      continue;
    }
    scanned += 1;
    if (refreshPipelineNextActionForRow_(sheet, index, rowNumber)) {
      updated += 1;
    }
  }

  return {
    ok: true,
    scanned,
    updated,
    updatedAt: new Date().toISOString(),
  };
}

function installPipelineEditTrigger() {
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach((trigger) => {
    if (trigger.getHandlerFunction() === 'handlePipelineEdit') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  ScriptApp.newTrigger('handlePipelineEdit')
    .forSpreadsheet(getRequiredConfig_('obfSpreadsheetId'))
    .onEdit()
    .create();
  return {
    ok: true,
    handler: 'handlePipelineEdit',
    spreadsheetId: getRequiredConfig_('obfSpreadsheetId'),
    installedAt: new Date().toISOString(),
  };
}

function syncPipelineToMessaging() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    return {
      ok: false,
      locked: true,
      message: 'Messaging sync is already running.',
    };
  }
  try {
    return syncPipelineToMessaging_();
  } finally {
    lock.releaseLock();
  }
}

function syncPipelineRowsToMessaging(prospectIds) {
  const ids = (Array.isArray(prospectIds) ? prospectIds : [])
    .map((id) => String(id || '').trim())
    .filter(Boolean);
  if (!ids.length) {
    return {
      ok: false,
      message: 'No Pipeline prospect IDs were selected for Messaging.',
    };
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    return {
      ok: false,
      locked: true,
      message: 'Messaging sync is already running.',
    };
  }
  try {
    return syncPipelineToMessaging_(ids);
  } finally {
    lock.releaseLock();
  }
}

function syncPipelineToMessaging_(prospectIds) {
  const pipelineSheet = getPipelineSheet_();
  const prospectsSheet = getProspectsSheet_();
  const messagingSheet = getMessagingSheet_();

  const pipelineHeaders = pipelineSheet.getRange(1, 1, 1, pipelineSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const prospectsHeaders = prospectsSheet.getRange(1, 1, 1, prospectsSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const messagingHeaders = messagingSheet.getRange(1, 1, 1, messagingSheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());

  const pipelineIndex = buildHeaderIndex_(pipelineHeaders);
  const prospectsIndex = buildHeaderIndex_(prospectsHeaders);
  const messagingIndex = buildHeaderIndex_(messagingHeaders);
  requireColumns_(pipelineIndex, ['Prospect ID', 'Company', 'Contact Name']);
  if (!contactLinkedInHeader_(pipelineIndex)) {
    throw new Error('Missing required Pipeline column: Contact Linkedin');
  }
  requireColumns_(prospectsIndex, ['ID', 'Website']);
  requireColumns_(messagingIndex, MESSAGING_REQUIRED_COLUMNS);

  const websiteById = buildProspectWebsiteById_(prospectsSheet, prospectsIndex);
  const messagingRowsById = buildMessagingRowsById_(messagingSheet, messagingIndex);
  const targetIds = prospectIds && prospectIds.length
    ? prospectIds.reduce((acc, id) => {
      acc[String(id || '').trim()] = true;
      return acc;
    }, {})
    : null;
  const pipelineLastRow = pipelineSheet.getLastRow();
  if (pipelineLastRow < 2) {
    return {
      ok: true,
      scanned: 0,
      inserted: 0,
      updated: 0,
      skippedExisting: 0,
    };
  }

  const pipelineRows = pipelineSheet
    .getRange(2, 1, pipelineLastRow - 1, pipelineHeaders.length)
    .getDisplayValues();
  const contactLinkedInHeader = contactLinkedInHeader_(pipelineIndex);
  const appendRows = [];
  let scanned = 0;
  let updated = 0;
  let skippedExisting = 0;
  let matched = 0;

  pipelineRows.forEach((row) => {
    const prospectId = cell_(row, pipelineIndex, 'Prospect ID');
    if (!prospectId) {
      return;
    }
    scanned += 1;
    if (targetIds && !targetIds[prospectId]) {
      return;
    }
    matched += 1;
    const mapped = {
      'Prospect ID': prospectId,
      Company: cell_(row, pipelineIndex, 'Company'),
      'Company Website': websiteById[prospectId] || '',
      'Contact Name': cell_(row, pipelineIndex, 'Contact Name'),
      'Contact Linkedin': normalizeUrl_(cell_(row, pipelineIndex, contactLinkedInHeader)),
    };
    const existingRowNumber = messagingRowsById[prospectId];
    if (existingRowNumber) {
      if (refreshMessagingRow_(messagingSheet, messagingIndex, existingRowNumber, mapped)) {
        updated += 1;
      } else {
        skippedExisting += 1;
      }
      return;
    }
    appendRows.push(messagingHeaders.map((header) => (mapped[header] !== undefined ? mapped[header] : '')));
  });

  if (appendRows.length) {
    const startRow = firstMessagingAppendRow_(messagingSheet, messagingIndex);
    messagingSheet.getRange(startRow, 1, appendRows.length, messagingHeaders.length).setValues(appendRows);
  }

  return {
    ok: true,
    scanned,
    matched,
    inserted: appendRows.length,
    updated,
    skippedExisting,
    missing: targetIds ? Math.max(0, Object.keys(targetIds).length - matched) : 0,
    syncedAt: new Date().toISOString(),
  };
}

function handlePipelineEdit(e) {
  if (!e || !e.range) {
    return;
  }
  const sheet = e.range.getSheet();
  if (sheet.getName() !== CONFIG.pipelineTabName || e.range.getRow() < 2) {
    return;
  }
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, PIPELINE_REQUIRED_COLUMNS);

  const editedColumn = e.range.getColumn();
  const progressColumn = index['Current Progress'] + 1;
  const lastActionColumn = index['Last Action Date'] + 1;
  if (editedColumn !== progressColumn && editedColumn !== lastActionColumn) {
    return;
  }

  if (editedColumn === progressColumn) {
    sheet.getRange(e.range.getRow(), lastActionColumn)
      .setValue(Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd'));
  }
  refreshPipelineNextActionForRow_(sheet, index, e.range.getRow());
}

function refreshPipelineNextActionForRow_(sheet, index, rowNumber) {
  const row = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const progress = cell_(row, index, 'Current Progress');
  const lastActionDate = cell_(row, index, 'Last Action Date');
  const schedule = buildPipelineNextStep_(progress, lastActionDate);
  sheet.getRange(rowNumber, index['Next Action'] + 1).setValue(schedule.nextAction);
  sheet.getRange(rowNumber, index['Next Action Due'] + 1)
    .setValue(schedule.nextActionDue);
  return Boolean(progress || lastActionDate);
}

function buildPipelineNextStep_(progress, lastActionDateValue) {
  const currentProgress = normalizePipelineProgress_(progress);
  const step = PIPELINE_SEQUENCE[currentProgress];
  if (!step) {
    return {
      nextAction: '',
      nextActionDue: '',
    };
  }

  const lastActionDate = parseSheetDate_(lastActionDateValue) || new Date();
  const nextActionDue = step.workingDays
    ? addWorkingDays_(lastActionDate, step.workingDays)
    : addCalendarDays_(lastActionDate, step.days || 0);
  return {
    nextAction: step.nextAction,
    nextActionDue: formatDateOnly_(nextActionDue),
  };
}

function normalizePipelineProgress_(progress) {
  const normalized = String(progress || '').trim().toLowerCase();
  if (!normalized) return '';
  const aliases = {
    connected: 'Connected',
    'first message': 'First Message',
    'first msg': 'First Message',
    m1: 'First Message',
    fail: 'Fail',
  };
  if (aliases[normalized]) {
    return aliases[normalized];
  }
  const followUp = normalized.match(/^fu[-\s]?(\d+)$/);
  if (followUp) {
    return `FU-${followUp[1]}`;
  }
  return String(progress || '').trim();
}

function parseSheetDate_(value) {
  if (Object.prototype.toString.call(value) === '[object Date]' && !isNaN(value.getTime())) {
    return copyDateOnly_(value);
  }
  const raw = String(value || '').trim();
  if (!raw) {
    return null;
  }
  const isoDate = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (isoDate) {
    return new Date(Number(isoDate[1]), Number(isoDate[2]) - 1, Number(isoDate[3]));
  }
  const date = new Date(raw);
  if (isNaN(date.getTime())) {
    return null;
  }
  return copyDateOnly_(date);
}

function addCalendarDays_(date, days) {
  const next = copyDateOnly_(date);
  next.setDate(next.getDate() + Number(days || 0));
  return next;
}

function addWorkingDays_(date, workingDays) {
  const next = copyDateOnly_(date);
  let remaining = Number(workingDays || 0);
  while (remaining > 0) {
    next.setDate(next.getDate() + 1);
    const day = next.getDay();
    if (day !== 0 && day !== 6) {
      remaining -= 1;
    }
  }
  return next;
}

function copyDateOnly_(date) {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

function formatDateOnly_(date) {
  return Utilities.formatDate(copyDateOnly_(date), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function classifyOutreachStatus_(status) {
  const normalized = String(status || '').trim().toLowerCase();
  if (!normalized) return 'new';
  if (normalized === 'connection sent') return 'connection_sent';
  if (normalized === 'connected') return 'connected';
  return 'other';
}

function buildManualRequestsPayload_(leads, counts, filter) {
  return {
    ok: true,
    prospectsTabName: CONFIG.prospectsTabName,
    leads,
    counts,
    filter: filter || 'all',
    pipelineGuard: getPipelineManualRequestGuard_(),
    generatedAt: new Date().toISOString(),
  };
}

function getPipelineManualRequestGuard_() {
  const sheet = getPipelineSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  const guard = {
    blocked: false,
    thresholdDays: 2,
    overdueCount: 0,
    worstOverdueDays: 0,
  };
  if (lastRow < 2 || lastColumn < 1) {
    return guard;
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  if (!('Next Action Due' in index) || !('Current Progress' in index)) {
    return guard;
  }
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  values.forEach((row) => {
    const progress = cell_(row, index, 'Current Progress');
    const dueInfo = buildPipelineDueInfo_(cell_(row, index, 'Next Action Due'), progress);
    if (typeof dueInfo.value !== 'number' || dueInfo.value > -2) {
      return;
    }
    guard.overdueCount += 1;
    guard.worstOverdueDays = Math.max(guard.worstOverdueDays, Math.abs(dueInfo.value));
  });
  guard.blocked = guard.overdueCount > 0;
  return guard;
}

function getOutreachLogPayload(options) {
  const opts = options || {};
  const filter = String(opts.filter || 'active').trim().toLowerCase();
  const search = String(opts.search || '').trim().toLowerCase();
  const limit = Math.max(1, Math.min(Number(opts.limit || CONFIG.outreachLogPageSize), 1000));

  const sheet = getOutreachLogSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return buildOutreachLogPayload_([], emptyOutreachLogCounts_(), filter, {}, search);
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, OUTREACH_LOG_REQUIRED_COLUMNS);
  const optionLists = {
    progressOptions: getValidationOptionsForColumn_(sheet, index['Current Progress'] + 1, OUTREACH_PROGRESS_VALUES),
    outcomeOptions: getValidationOptionsForColumn_(sheet, index['Outcome'] + 1, OUTREACH_OUTCOME_VALUES),
  };
  const prospectLinkedInById = buildProspectLinkedInById_();
  const prospectPeopleById = buildProspectPeopleById_();

  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const counts = emptyOutreachLogCounts_();
  const entries = [];

  for (let i = 0; i < values.length; i++) {
    const row = values[i];
    const prospectId = cell_(row, index, 'Prospect ID');
    const company = cell_(row, index, 'Company');
    if (!prospectId && !company) {
      continue;
    }
    const progress = cell_(row, index, 'Current Progress');
    const progressKey = classifyProgress_(progress);
    const daysInfo = buildDaysLeftInfo_(cell_(row, index, 'Days Left'), cell_(row, index, 'Sent At'), progressKey);
    const contactLinkedInHeader = contactLinkedInHeader_(index);
    const contactLinkedIn = normalizeUrl_(contactLinkedInHeader ? cell_(row, index, contactLinkedInHeader) : '')
      || prospectLinkedInById[prospectId]
      || '';
    const prospectPeople = prospectPeopleById[prospectId] || {};
    counts.all += 1;
    if (progressKey === 'conn_request') counts.conn_request += 1;
    else if (progressKey === 'connected') counts.connected += 1;
    else if (progressKey === 'withdrawn') counts.withdrawn += 1;
    if (daysInfo.overdue) counts.overdue += 1;

    const entry = {
      rowNumber: i + 2,
      prospectId,
      company,
      personEngaged: cell_(row, index, 'Person Engaged'),
      contactName: cell_(row, index, 'Contact Name'),
      p1Name: prospectPeople.p1Name || '',
      p2Name: prospectPeople.p2Name || '',
      contactLinkedIn,
      currentProgress: progress,
      progressKey,
      outcome: cell_(row, index, 'Outcome'),
      sentAt: cell_(row, index, 'Sent At'),
      daysLeft: daysInfo.display,
      daysLeftValue: daysInfo.value,
      countdownTone: daysInfo.tone,
      notes: cell_(row, index, 'Notes'),
    };

    if (!outreachLogMatchesFilter_(filter, progressKey, daysInfo)) {
      continue;
    }
    if (search && !outreachLogMatchesSearch_(entry, search)) {
      continue;
    }
    if (entries.length >= limit) {
      continue;
    }
    entries.push(entry);
  }

  return buildOutreachLogPayload_(entries, counts, filter, optionLists, search);
}

function updateOutreachLog(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return { ok: true, processed: 0, updatedAt: new Date().toISOString() };
  }
  const sheet = getOutreachLogSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Current Progress', 'Outcome', 'Last Action Date']);

  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  const lastRow = sheet.getLastRow();
  let pushedToPipeline = 0;
  rows.forEach((item) => {
    const rowNumber = Number(item && item.rowNumber);
    if (!Number.isInteger(rowNumber) || rowNumber < 2 || rowNumber > lastRow) {
      throw new Error(`Invalid Outreach Log row number: ${item && item.rowNumber}`);
    }
    const existingRowValues = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
    const oldProgress = cell_(existingRowValues, index, 'Current Progress');
    if (Object.prototype.hasOwnProperty.call(item, 'currentProgress')) {
      const nextProgress = String(item.currentProgress || '').trim();
      sheet.getRange(rowNumber, index['Current Progress'] + 1).setValue(nextProgress);
      if (classifyProgress_(oldProgress) !== classifyProgress_(nextProgress)
        && classifyProgress_(nextProgress) === 'withdrawn') {
        incrementDailyMetrics_({ 'Requests Withdrawn': 1 });
      }
    }
    if (Object.prototype.hasOwnProperty.call(item, 'outcome')) {
      sheet.getRange(rowNumber, index['Outcome'] + 1)
        .setValue(String(item.outcome || '').trim());
    }
    sheet.getRange(rowNumber, index['Last Action Date'] + 1).setValue(today);

    const updatedRow = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
    const updatedProgress = cell_(updatedRow, index, 'Current Progress');
    if (classifyProgress_(updatedProgress) === 'connected') {
      const pipelineResult = upsertPipelineFromOutreachLog_(updatedRow, index);
      if (pipelineResult && pipelineResult.pipelineInserted) {
        incrementDailyMetrics_({ 'New Pipeline': 1 });
      }
      pushedToPipeline += 1;
    }
  });

  return {
    ok: true,
    processed: rows.length,
    pushedToPipeline,
    updatedAt: new Date().toISOString(),
  };
}

function backfillConnectedOutreachLogDaysLeft() {
  const sheet = getOutreachLogSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {
      ok: true,
      scanned: 0,
      updated: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Current Progress', 'Days Left']);

  let scanned = 0;
  let updated = 0;
  for (let rowNumber = 2; rowNumber <= lastRow; rowNumber++) {
    const row = sheet.getRange(rowNumber, 1, 1, lastColumn).getDisplayValues()[0];
    const progress = cell_(row, index, 'Current Progress');
    if (!progress) {
      continue;
    }
    scanned += 1;
    if (classifyProgress_(progress) !== 'connected') {
      continue;
    }
    if (!cell_(row, index, 'Days Left')) {
      continue;
    }
    syncOutreachDaysLeftForProgress_(sheet, index, rowNumber, progress);
    updated += 1;
  }

  return {
    ok: true,
    scanned,
    updated,
    updatedAt: new Date().toISOString(),
  };
}

function syncOutreachDaysLeftForProgress_(sheet, index, rowNumber, progress) {
  if (!('Days Left' in index)) {
    return false;
  }
  if (classifyProgress_(progress) !== 'connected') {
    return false;
  }
  sheet.getRange(rowNumber, index['Days Left'] + 1).clearContent();
  return true;
}

function writeMappedRowSkippingColumns_(sheet, rowNumber, headers, mapped, skippedHeaders) {
  const skipped = {};
  (skippedHeaders || []).forEach((header) => {
    skipped[header] = true;
  });

  let startIndex = -1;
  let values = [];
  for (let i = 0; i <= headers.length; i++) {
    const header = headers[i];
    const shouldWrite = i < headers.length && !skipped[header];
    if (shouldWrite) {
      if (startIndex === -1) {
        startIndex = i;
      }
      values.push(mapped[header] !== undefined ? mapped[header] : '');
      continue;
    }

    if (startIndex !== -1) {
      sheet.getRange(rowNumber, startIndex + 1, 1, values.length).setValues([values]);
      startIndex = -1;
      values = [];
    }
  }
}

function emptyOutreachLogCounts_() {
  return { all: 0, conn_request: 0, connected: 0, withdrawn: 0, overdue: 0 };
}

function buildOutreachLogPayload_(entries, counts, filter, optionLists, search) {
  const options = optionLists || {};
  return {
    ok: true,
    outreachLogTabName: CONFIG.outreachLogTabName,
    entries,
    counts,
    filter: filter || 'active',
    search: search || '',
    progressOptions: options.progressOptions || [],
    outcomeOptions: options.outcomeOptions || [],
    generatedAt: new Date().toISOString(),
  };
}

function outreachLogMatchesSearch_(entry, search) {
  const haystack = [
    entry.contactName,
    entry.p1Name,
    entry.p2Name,
  ].join(' ').toLowerCase();
  return haystack.indexOf(search) !== -1;
}

function outreachLogMatchesFilter_(filter, progressKey, daysInfo) {
  if (filter === 'all') return true;
  if (filter === 'active') return true;
  if (filter === 'overdue') return Boolean(daysInfo.overdue);
  return filter === progressKey;
}

function classifyProgress_(progress) {
  const normalized = String(progress || '').trim().toLowerCase();
  if (!normalized) return 'blank';
  if (normalized === 'conn request' || normalized === 'connection request') return 'conn_request';
  if (normalized === 'connected') return 'connected';
  if (normalized === 'withdrawn' || normalized === 'request withdrawn') return 'withdrawn';
  if (normalized === 'first message') return 'first_message';
  if (/^fu-\d+$/.test(normalized)) return 'follow_up';
  if (normalized === 'follow up' || normalized === 'follow-up') return 'follow_up';
  if (normalized === 'not interested') return 'not_interested';
  return 'other';
}

function buildDaysLeftInfo_(daysLeftDisplay, sentAtDisplay, progressKey) {
  const display = String(daysLeftDisplay || '').trim();
  if (progressKey === 'connected') {
    return {
      display: '-',
      value: '',
      overdue: false,
      tone: 'neutral',
    };
  }
  const directNumber = parseDaysLeftNumber_(display);
  const fallbackNumber = directNumber === null ? daysLeftFromSentAt_(sentAtDisplay) : directNumber;
  if (fallbackNumber === null) {
    return {
      display,
      value: '',
      overdue: false,
      tone: 'blank',
    };
  }
  const overdue = fallbackNumber <= 0 || /overdue/i.test(display);
  let tone = 'green';
  if (overdue) tone = 'red';
  else if (fallbackNumber <= 7) tone = 'amber';
  return {
    display: display || (overdue ? 'Overdue' : `${fallbackNumber} days left`),
    value: fallbackNumber,
    overdue,
    tone,
  };
}

function parseDaysLeftNumber_(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  if (text === '-') return null;
  if (/^\d{4}-\d{1,2}-\d{1,2}$/.test(text)) return null;
  if (/overdue/i.test(text)) return 0;
  const match = text.match(/-?\d+/);
  return match ? Number(match[0]) : null;
}

function daysLeftFromSentAt_(sentAtDisplay) {
  const sentAt = String(sentAtDisplay || '').trim();
  if (!sentAt) return null;
  const date = new Date(sentAt);
  if (isNaN(date.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  date.setHours(0, 0, 0, 0);
  const elapsed = Math.floor((today.getTime() - date.getTime()) / (1000 * 60 * 60 * 24));
  return 14 - elapsed;
}

function buildProspectLinkedInById_() {
  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {};
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  if (!('ID' in index)) {
    return {};
  }
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const map = {};
  values.forEach((row) => {
    const id = cell_(row, index, 'ID');
    if (!id) return;
    const engagedPerson = cell_(row, index, 'Engaged Person');
    map[id] = contactLinkedInFromProspectRow_(row, index, engagedPerson);
  });
  return map;
}

function buildProspectPeopleById_() {
  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {};
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  if (!('ID' in index)) {
    return {};
  }
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const map = {};
  values.forEach((row) => {
    const id = cell_(row, index, 'ID');
    if (!id) return;
    map[id] = {
      p1Name: cell_(row, index, 'P1 Name'),
      p2Name: cell_(row, index, 'P2 Name'),
    };
  });
  return map;
}

function buildProspectContactsById_() {
  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {};
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  if (!('ID' in index)) {
    return {};
  }
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const map = {};
  values.forEach((row) => {
    const id = cell_(row, index, 'ID');
    if (!id) return;
    map[id] = {
      p1Name: cell_(row, index, 'P1 Name'),
      p1Linkedin: normalizeUrl_(cell_(row, index, 'P1 LinkedIn')),
      p2Name: cell_(row, index, 'P2 Name'),
      p2Linkedin: normalizeUrl_(cell_(row, index, 'P2 LinkedIn')),
      engagedPerson: cell_(row, index, 'Engaged Person'),
    };
  });
  return map;
}

function resolveContactLinkedInFromProspectLookup_(prospectContactsById, prospectId, engagedPerson, contactName) {
  const prospectContact = prospectContactsById[String(prospectId || '').trim()];
  if (!prospectContact) {
    return '';
  }
  return contactLinkedInFromProspectContact_(
    prospectContact,
    engagedPerson || prospectContact.engagedPerson,
    contactName,
  );
}

function buildProspectWebsiteById_() {
  const sheet = getProspectsSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return {};
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  if (!('ID' in index) || !('Website' in index)) {
    return {};
  }
  const values = sheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const map = {};
  values.forEach((row) => {
    const id = cell_(row, index, 'ID');
    if (!id) return;
    map[id] = normalizeUrl_(cell_(row, index, 'Website'));
  });
  return map;
}

function buildMessagingRowsById_(sheet, index) {
  if (!('Prospect ID' in index)) {
    return {};
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return {};
  }
  const values = sheet.getRange(2, index['Prospect ID'] + 1, lastRow - 1, 1).getDisplayValues();
  const map = {};
  values.forEach((row, i) => {
    const prospectId = String(row[0] || '').trim();
    if (prospectId && !(prospectId in map)) {
      map[prospectId] = i + 2;
    }
  });
  return map;
}

function firstMessagingAppendRow_(sheet, index) {
  const keyColumns = ['Prospect ID', 'Company', 'Contact Name']
    .filter((column) => column in index)
    .map((column) => index[column] + 1);
  if (!keyColumns.length) {
    return firstAppendRow_(sheet, sheet.getLastColumn());
  }
  const lastRow = Math.max(1, sheet.getLastRow());
  if (lastRow < 2) {
    return 2;
  }
  let lastMeaningful = 1;
  keyColumns.forEach((columnNumber) => {
    const values = sheet.getRange(2, columnNumber, lastRow - 1, 1).getDisplayValues();
    for (let i = 0; i < values.length; i++) {
      if (String(values[i][0] || '').trim()) {
        lastMeaningful = Math.max(lastMeaningful, i + 2);
      }
    }
  });
  return lastMeaningful + 1;
}

function refreshMessagingRow_(sheet, index, rowNumber, mapped) {
  let changed = false;
  Object.keys(mapped).forEach((column) => {
    if (!(column in index)) {
      return;
    }
    const cell = sheet.getRange(rowNumber, index[column] + 1);
    const current = String(cell.getDisplayValue() || '').trim();
    const next = String(mapped[column] || '').trim();
    if (next && current !== next) {
      cell.setValue(next);
      changed = true;
    }
  });
  return changed;
}

function contactLinkedInHeader_(index) {
  if ('Contact Linkedin' in index) return 'Contact Linkedin';
  if ('Contact LinkedIn' in index) return 'Contact LinkedIn';
  if ('Contact LinkedIn URL' in index) return 'Contact LinkedIn URL';
  return '';
}

function getValidationOptionsForColumn_(sheet, columnNumber, fallbackOptions) {
  const lastRow = Math.max(sheet.getLastRow(), 2);
  const validations = sheet.getRange(2, columnNumber, lastRow - 1, 1).getDataValidations();
  for (let i = 0; i < validations.length; i++) {
    const validation = validations[i][0];
    const options = validationOptions_(validation);
    if (options.length) {
      return options;
    }
  }
  const headerValidation = sheet.getRange(1, columnNumber).getDataValidation();
  const headerOptions = validationOptions_(headerValidation);
  return headerOptions.length ? headerOptions : (fallbackOptions || []);
}

function validationOptions_(validation) {
  if (!validation) {
    return [];
  }
  const criteriaType = validation.getCriteriaType();
  const values = validation.getCriteriaValues();
  if (criteriaType === SpreadsheetApp.DataValidationCriteria.VALUE_IN_LIST) {
    return uniqueNonBlank_(values[0] || []);
  }
  if (criteriaType === SpreadsheetApp.DataValidationCriteria.VALUE_IN_RANGE && values[0]) {
    return uniqueNonBlank_(values[0].getDisplayValues().flat());
  }
  return [];
}

function uniqueNonBlank_(values) {
  const seen = {};
  const output = [];
  values.forEach((value) => {
    const text = String(value || '').trim();
    if (!text || seen[text]) {
      return;
    }
    seen[text] = true;
    output.push(text);
  });
  return output;
}

function mergeOptions_(primary, secondary) {
  return uniqueNonBlank_((primary || []).concat(secondary || []));
}

function getReviewPayload(options) {
  const opts = options || {};
  const sheet = getReviewSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return buildReviewPayload_([], '', sheet);
  }
  const headers = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, REVIEW_REQUIRED_COLUMNS);

  const group = latestReviewGroupRange_(sheet, index, lastRow, opts);
  if (group.endRow <= group.startRow) {
    return buildReviewPayload_([], group.dateLabel, sheet);
  }
  const values = sheet.getRange(group.startRow, 1, group.endRow - group.startRow, lastColumn).getDisplayValues();
  const leads = [];
  for (let i = 0; i < values.length; i++) {
    const row = values[i];
    const runId = cell_(row, index, 'Run ID');
    const company = cell_(row, index, 'Company Name');
    if (!runId && !company) {
      continue;
    }
    leads.push({
      rowNumber: group.startRow + i,
      runId,
      company,
      website: normalizeUrl_(cell_(row, index, 'Company Website')),
      empCount: cell_(row, index, 'Emp Count'),
      approved: checkboxTruthy_(cell_(row, index, 'Approved')),
      use: cell_(row, index, 'Use'),
    });
  }

  return buildReviewPayload_(leads, group.dateLabel, sheet);
}

function updateReviewApprovals(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return { ok: true, processed: 0, approvedCount: 0, updatedAt: new Date().toISOString() };
  }

  const sheet = getReviewSheet_();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  requireColumns_(index, REVIEW_REQUIRED_COLUMNS);

  let approvedCount = 0;
  rows.forEach((item) => {
    const rowNumber = Number(item && item.rowNumber);
    if (!Number.isInteger(rowNumber) || rowNumber < 2 || rowNumber > sheet.getLastRow()) {
      throw new Error(`Invalid review row number: ${item && item.rowNumber}`);
    }
    const approved = Boolean(item && item.approved);
    if (approved) {
      approvedCount += 1;
    }
    sheet.getRange(rowNumber, index.Approved + 1).setValue(approved);
    sheet.getRange(rowNumber, index.Use + 1).setValue(String((item && item.use) || '').trim());
  });

  return {
    ok: true,
    processed: rows.length,
    approvedCount,
    updatedAt: new Date().toISOString(),
  };
}

function getSourceSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.sourceTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.sourceTabName}`);
  }
  return sheet;
}

function getFinalSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.finalTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.finalTabName}`);
  }
  return sheet;
}

function getReviewSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.reviewTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.reviewTabName}`);
  }
  return sheet;
}

function getProspectsSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.prospectsTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.prospectsTabName}`);
  }
  return sheet;
}

function getOutreachControlSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.outreachControlTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.outreachControlTabName}`);
  }
  return sheet;
}

function getOutreachLogSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.outreachLogTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.outreachLogTabName}`);
  }
  return sheet;
}

function getPipelineSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.pipelineTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.pipelineTabName}`);
  }
  return sheet;
}

function getMessagingSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.messagingTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.messagingTabName}`);
  }
  return sheet;
}

function getMessageDraftsSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  let sheet = spreadsheet.getSheetByName(CONFIG.messagingDraftsTabName);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(CONFIG.messagingDraftsTabName);
  }
  ensureMessageDraftHeaders_(sheet);
  return sheet;
}

function getMessagingTemplatesSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  let sheet = spreadsheet.getSheetByName(CONFIG.messagingTemplatesTabName);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(CONFIG.messagingTemplatesTabName);
    seedMessagingTemplates_(sheet);
  } else if (sheet.getLastRow() < 2) {
    seedMessagingTemplates_(sheet);
  }
  return sheet;
}

function seedMessagingTemplates_(sheet) {
  const headers = ['Template ID', 'Stage', 'Enabled', 'Message Body', 'Notes'];
  const rows = [
    ['FM-01', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nWould you be open to a phased redesign of the current [Company] website?\\n\\nThe first phase would give you:\\n- A detailed teardown of the current website and brand presence\\n- A clear creative direction\\n- A redesign of the most important page\\n\\nNo commitment upfront. The goal is to make the opportunity visible first, then you only move forward if the direction feels clearly worth it.\\n\\nMay I send over the detailed PDF teardown first? Or if you prefer, I can record a short Loom video.\\n\\nPS: Everything created in phase one is yours to keep, regardless of what happens.\\n\\nBest,\\nTony', 'Replace with final variation 1'],
    ['FM-02', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nI put together a short visual breakdown for [Company] showing a few website and positioning gaps that may be costing you indirectly during visitor due diligence.\\n\\nThe issues were specific enough to be worth showing visually rather than explaining in a long message.\\n\\nWould you be open to me sending it over?\\n\\nNo strings attached. If it is useful, you might think of me when [Company] is ready for a digital overhaul. If not, the insights are yours to keep.\\n\\nBest,\\nTony', 'Visual breakdown angle'],
    ['FM-03', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nI noticed a few places where [Company] could make a stronger first impression before a prospect or partner ever speaks with the team.\\n\\nRather than send a generic pitch, I mapped the points visually so you can judge them quickly.\\n\\nWould it be okay if I sent the breakdown across?\\n\\nIf it is useful, great. If not, you still have the notes.\\n\\nBest,\\nTony', 'First impression angle'],
    ['FM-04', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nI reviewed [Company] from the perspective of a visitor checking credibility, clarity, and next steps. A few small structural issues stood out.\\n\\nI turned them into a concise visual audit so it is easier to review.\\n\\nOpen to me sending it over?\\n\\nNo obligation. I mainly want to make the opportunity visible first.\\n\\nBest,\\nTony', 'Credibility audit angle'],
    ['FM-05', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nI had a look at [Company] and spotted a few presentation gaps that could quietly weaken trust with high-intent visitors.\\n\\nI can send a short visual teardown showing exactly what I mean, with no long explanation needed.\\n\\nWould you be open to seeing it?\\n\\nIf it helps, we can talk later. If not, the teardown is yours to keep.\\n\\nBest,\\nTony', 'Trust gap angle'],
    ['FM-06', 'First Message', 'yes', 'Hi [First Name], thanks for connecting.\\n\\nI put together a quick outside-in review of [Company] focused on where the website could communicate the firm’s value more clearly.\\n\\nIt is short, visual, and specific to what I saw.\\n\\nMay I send it over?\\n\\nNo pressure at all. My thinking is simple: useful first, conversation later only if it earns one.\\n\\nBest,\\nTony', 'Outside-in review angle'],
  ];
  sheet.clearContents();
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
}

function getDailyMetricsSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('obfSpreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.dailyMetricsTabName);
  if (!sheet) {
    throw new Error(`Missing sheet tab: ${CONFIG.dailyMetricsTabName}`);
  }
  return sheet;
}

function getOrCreateSanitizeLogSheet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  let sheet = spreadsheet.getSheetByName(CONFIG.sanitizeLogTabName);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(CONFIG.sanitizeLogTabName);
    sheet.getRange(1, 1, 1, SANITIZE_LOG_HEADERS.length).setValues([SANITIZE_LOG_HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function appendSanitizeLogEntries_(entries) {
  if (!entries || !entries.length) {
    return;
  }
  const sheet = getOrCreateSanitizeLogSheet_();
  const headers = sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), SANITIZE_LOG_HEADERS.length)).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const usableHeaders = headers.length >= SANITIZE_LOG_HEADERS.length ? headers : SANITIZE_LOG_HEADERS;
  const fieldByHeader = {
    'Run At': 'runAt',
    'Run ID': 'runId',
    'Action': 'action',
    'Lead ID': 'leadId',
    'Company': 'company',
    'New P1 Name': 'newP1Name',
    'Old P1 Name': 'oldP1Name',
    'Match Reason': 'reason',
  };
  const payload = entries.map((entry) => usableHeaders.map((header) => {
    const field = fieldByHeader[header];
    return field && entry[field] !== undefined ? entry[field] : '';
  }));
  const startRow = Math.max(2, sheet.getLastRow() + 1);
  sheet.getRange(startRow, 1, payload.length, usableHeaders.length).setValues(payload);
}

function getSanitizeLogPayload(options) {
  const opts = options || {};
  const sheet = getOrCreateSanitizeLogSheet_();
  const lastRow = sheet.getLastRow();
  const lastColumn = Math.max(sheet.getLastColumn(), SANITIZE_LOG_HEADERS.length);
  if (lastRow < 2) {
    return buildSanitizeLogPayload_([], sheet);
  }
  const headerValues = sheet.getRange(1, 1, 1, lastColumn).getDisplayValues()[0]
    .map((header) => String(header).trim());
  const index = buildHeaderIndex_(headerValues);
  const pageSize = Math.max(1, Math.min(Number(opts.limit || CONFIG.sanitizeLogPageSize), 500));
  const rowCount = lastRow - 1;
  const startRow = Math.max(2, lastRow - pageSize + 1);
  const readCount = lastRow - startRow + 1;
  const values = sheet.getRange(startRow, 1, readCount, lastColumn).getDisplayValues();
  const entries = [];
  for (let i = values.length - 1; i >= 0; i--) {
    const row = values[i];
    const runAt = cell_(row, index, 'Run At');
    const action = cell_(row, index, 'Action');
    if (!runAt && !action) {
      continue;
    }
    entries.push({
      rowNumber: startRow + i,
      runAt,
      runId: cell_(row, index, 'Run ID'),
      action,
      leadId: cell_(row, index, 'Lead ID'),
      company: cell_(row, index, 'Company'),
      newP1Name: cell_(row, index, 'New P1 Name'),
      oldP1Name: cell_(row, index, 'Old P1 Name'),
      reason: cell_(row, index, 'Match Reason'),
    });
  }
  return buildSanitizeLogPayload_(entries, sheet, { totalRows: rowCount });
}

function buildSanitizeLogPayload_(entries, sheet, meta) {
  const totals = entries.reduce((acc, entry) => {
    if (entry.action === 'Swapped') acc.swapped += 1;
    else if (entry.action === 'Moved') acc.moved += 1;
    return acc;
  }, { swapped: 0, moved: 0 });
  const runs = new Set();
  entries.forEach((entry) => {
    if (entry.runId) runs.add(entry.runId);
  });
  return {
    ok: true,
    sanitizeLogTabName: CONFIG.sanitizeLogTabName,
    entries,
    counts: {
      shown: entries.length,
      swapped: totals.swapped,
      moved: totals.moved,
      runs: runs.size,
    },
    totalRows: (meta && meta.totalRows) || entries.length,
    generatedAt: new Date().toISOString(),
  };
}

function getWorkflowState() {
  return getWorkflowState_();
}

function getWorkflowState_() {
  const raw = PropertiesService.getScriptProperties().getProperty(workflowStateKey_());
  const base = {
    dateKey: workflowDateKey_(),
    sanitizeDone: false,
    bridgeFinalDone: false,
    bridgeProspectsDone: false,
  };
  if (!raw) {
    return base;
  }
  try {
    return Object.assign(base, JSON.parse(raw));
  } catch (error) {
    return base;
  }
}

function saveWorkflowState_(patch) {
  const state = Object.assign({}, getWorkflowState_(), patch || {}, {
    dateKey: workflowDateKey_(),
    updatedAt: new Date().toISOString(),
  });
  PropertiesService.getScriptProperties().setProperty(workflowStateKey_(), JSON.stringify(state));
  return state;
}

function workflowStateKey_() {
  return `lead_activity_state_${workflowDateKey_()}`;
}

function workflowDateKey_() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function latestReviewGroupRange_(sheet, index, lastRow, options) {
  const opts = options || {};
  const pageSize = Math.max(1, Math.min(Number(opts.limit || CONFIG.reviewPageSize), 250));
  if (!('Date' in index)) {
    return {
      dateLabel: '',
      startRow: 2,
      endRow: lastRow + 1,
    };
  }

  const rowCount = Math.max(0, lastRow - 1);
  const dateColumn = index.Date + 1;
  const dateValues = rowCount
    ? sheet.getRange(2, dateColumn, rowCount, 1).getDisplayValues()
    : [];
  let groupRow = 0;
  let dateLabel = '';
  for (let i = dateValues.length - 1; i >= 0; i--) {
    const value = String(dateValues[i][0] || '').trim();
    if (value) {
      groupRow = i + 2;
      dateLabel = value;
      break;
    }
  }

  if (!groupRow) {
    return {
      dateLabel: '',
      startRow: 2,
      endRow: lastRow + 1,
    };
  }

  return {
    dateLabel,
    startRow: groupRow + 1,
    endRow: Math.min(lastRow + 1, groupRow + 1 + pageSize),
  };
}

function buildHeaderIndex_(headers) {
  const index = {};
  headers.forEach((header, i) => {
    if (header) {
      index[header] = i;
    }
  });
  return index;
}

function requireColumns_(index, columns) {
  const missing = columns.filter((column) => !(column in index));
  if (missing.length) {
    throw new Error(`Missing required columns: ${missing.join(', ')}`);
  }
}

function leadFromRow_(row, index, rowNumber) {
  return {
    rowNumber,
    id: cell_(row, index, 'ID'),
    company: cell_(row, index, 'Company'),
    website: normalizeUrl_(cell_(row, index, 'Website')),
    companyLinkedIn: normalizeUrl_(cell_(row, index, 'Company LinkedIn')),
    empCount: cell_(row, index, 'Emp Count'),
    sourceTab: cell_(row, index, 'Source Tab'),
    use: cell_(row, index, 'Use'),
    category: cell_(row, index, 'Category'),
    p1: personFromRow_(row, index, 'P1'),
    p2: personFromRow_(row, index, 'P2'),
  };
}

function personFromRow_(row, index, prefix) {
  return {
    name: cell_(row, index, `${prefix} Name`),
    title: cell_(row, index, `${prefix} Title`),
    linkedin: normalizeUrl_(cell_(row, index, `${prefix} LinkedIn`)),
    email: cell_(row, index, `${prefix} Email`),
    activity: cell_(row, index, `${prefix} Activity`),
  };
}

function cell_(row, index, column) {
  if (!(column in index)) {
    return '';
  }
  return String(row[index[column]] || '').trim();
}

function readBlockedSet_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('comparisonSpreadsheetId'));
  const sheet = getSheetByGidOrFirst_(spreadsheet, getRequiredConfig_('comparisonSheetGid'));
  const values = sheet.getDataRange().getDisplayValues();
  if (!values.length) {
    throw new Error('Comparison sheet is empty.');
  }
  const headers = values[0].map((header) => String(header).trim());
  const index = buildHeaderIndex_(headers);
  const nameColumn = firstExistingColumn_(headers, NAME_COLUMN_CANDIDATES);
  const linkedinColumn = findFirstColumn_(headers, LINKEDIN_COLUMN_CANDIDATES);
  const names = new Set();
  const linkedins = new Set();
  for (let r = 1; r < values.length; r++) {
    const name = normalizeName_(values[r][index[nameColumn]]);
    if (name) {
      names.add(name);
    }
    if (linkedinColumn) {
      const url = normalizeLinkedInForMatch_(values[r][index[linkedinColumn]]);
      if (url) {
        linkedins.add(url);
      }
    }
  }
  return {
    names,
    linkedins,
    sheetName: sheet.getName(),
    nameColumn,
    linkedinColumn,
  };
}

function findFirstColumn_(headers, candidates) {
  const exact = buildHeaderIndex_(headers);
  for (let i = 0; i < candidates.length; i++) {
    if (candidates[i] in exact) {
      return candidates[i];
    }
  }
  const lowered = {};
  headers.forEach((header) => {
    lowered[String(header || '').trim().toLowerCase()] = header;
  });
  for (let i = 0; i < candidates.length; i++) {
    const found = lowered[candidates[i].toLowerCase()];
    if (found) {
      return found;
    }
  }
  return '';
}

function normalizeLinkedInForMatch_(value) {
  const url = normalizeUrl_(value);
  if (!url) return '';
  if (!/linkedin\.com\/in\//i.test(url)) return '';
  return url.toLowerCase();
}

function getSheetByGidOrFirst_(spreadsheet, gid) {
  const target = String(gid || '');
  const sheets = spreadsheet.getSheets();
  for (let i = 0; i < sheets.length; i++) {
    if (String(sheets[i].getSheetId()) === target) {
      return sheets[i];
    }
  }
  return sheets[0];
}

function firstExistingColumn_(headers, candidates) {
  const exact = buildHeaderIndex_(headers);
  for (let i = 0; i < candidates.length; i++) {
    if (candidates[i] in exact) {
      return candidates[i];
    }
  }
  const lowered = {};
  headers.forEach((header) => {
    lowered[String(header || '').trim().toLowerCase()] = header;
  });
  for (let i = 0; i < candidates.length; i++) {
    const found = lowered[candidates[i].toLowerCase()];
    if (found) {
      return found;
    }
  }
  throw new Error(`Could not find a name column. Tried: ${candidates.join(', ')}`);
}

function normalizeName_(value) {
  return String(value || '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function rowHasP2_(row, index) {
  return P2_COLUMNS.some((column) => cell_(row, index, column));
}

function appendRowsToCopy_(copySheet, copyHeaders, sourceHeaders, rows) {
  if (!rows.length) {
    return;
  }
  const sourceIndex = buildHeaderIndex_(sourceHeaders);
  const payload = rows.map((row) => copyHeaders.map((header) => {
    return header in sourceIndex ? row[sourceIndex[header]] || '' : '';
  }));
  const startRow = firstAppendRow_(copySheet, copyHeaders.length);
  copySheet.getRange(startRow, 1, payload.length, copyHeaders.length).setValues(payload);
}

function rewriteSheetBody_(sheet, columnCount, rows) {
  const maxRows = sheet.getMaxRows();
  if (maxRows > 1) {
    sheet.getRange(2, 1, maxRows - 1, columnCount).clearContent();
  }
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, columnCount).setValues(rows.map((row) => row.slice(0, columnCount)));
  }
}

function firstAppendRow_(sheet, columnCount) {
  const lastRow = Math.max(1, sheet.getLastRow());
  if (lastRow < 2) {
    return 2;
  }
  const values = sheet.getRange(2, 1, lastRow - 1, columnCount).getDisplayValues();
  let lastMeaningful = 1;
  for (let i = 0; i < values.length; i++) {
    if (values[i].some((value) => String(value || '').trim())) {
      lastMeaningful = i + 2;
    }
  }
  return lastMeaningful + 1;
}

function normalizeUrl_(value) {
  let raw = String(value || '').trim();
  if (!raw) {
    return '';
  }
  if (/^\/\//.test(raw)) {
    raw = `https:${raw}`;
  } else if (!/^https?:\/\//i.test(raw)) {
    raw = `https://${raw}`;
  }

  return normalizeLinkedInProfileUrl_(raw);
}

function normalizeLinkedInProfileUrl_(url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    const linkedInLikeHost = host === 'linkedin.com' || host.endsWith('.linkedin.com') || host.startsWith('linkedin.');
    if (!linkedInLikeHost || !/^\/in\/[^/]+\/?/i.test(parsed.pathname)) {
      return url;
    }
    const pathParts = parsed.pathname.split('/').filter(Boolean);
    const slug = pathParts.length >= 2 ? pathParts[1] : '';
    if (!slug) {
      return url;
    }
    return `https://www.linkedin.com/in/${slug}`;
  } catch (error) {
    return url;
  }
}

function normalizeActivityValue_(value) {
  const raw = String(value || '').trim();
  if (ACTIVITY_VALUES.indexOf(raw) === -1) {
    throw new Error(`Unsupported activity value: ${raw}`);
  }
  return raw;
}

function checkboxTruthy_(value) {
  return ['true', 'yes', 'y', '1', 'checked'].indexOf(String(value || '').trim().toLowerCase()) !== -1;
}

function leadMatchesSearch_(lead, search) {
  const haystack = [
    lead.id,
    lead.company,
    lead.category,
    lead.p1.name,
    lead.p1.title,
    lead.p2.name,
    lead.p2.title,
  ].join(' ').toLowerCase();
  return haystack.indexOf(search) !== -1;
}

function getFinalIds_(finalSheet) {
  const values = finalSheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    return new Set();
  }
  const index = buildHeaderIndex_(values[0].map((header) => String(header).trim()));
  if (!('ID' in index)) {
    return new Set();
  }
  const ids = new Set();
  for (let r = 1; r < values.length; r++) {
    const id = String(values[r][index.ID] || '').trim();
    if (id) {
      ids.add(id);
    }
  }
  return ids;
}

function getCopyIds_() {
  const spreadsheet = SpreadsheetApp.openById(getRequiredConfig_('spreadsheetId'));
  const sheet = spreadsheet.getSheetByName(CONFIG.copyTabName);
  if (!sheet || sheet.getLastRow() < 2) {
    return new Set();
  }
  const values = sheet.getDataRange().getDisplayValues();
  const index = buildHeaderIndex_(values[0].map((header) => String(header).trim()));
  if (!('ID' in index)) {
    return new Set();
  }
  const ids = new Set();
  for (let r = 1; r < values.length; r++) {
    const id = String(values[r][index.ID] || '').trim();
    if (id) {
      ids.add(id);
    }
  }
  return ids;
}

function getProspectsIds_(prospectsSheet, prospectsIndex) {
  if (!('ID' in prospectsIndex)) {
    return new Set();
  }
  const rowCount = Math.max(0, prospectsSheet.getLastRow() - 1);
  if (rowCount < 1) {
    return new Set();
  }
  const values = prospectsSheet.getRange(2, prospectsIndex.ID + 1, rowCount, 1).getDisplayValues();
  const ids = new Set();
  values.forEach((row) => {
    const id = String(row[0] || '').trim();
    if (id) {
      ids.add(id);
    }
  });
  return ids;
}

function recordOutreachControlProspectsStartRow_(startRow) {
  const sheet = getOutreachControlSheet_();
  const values = sheet.getDataRange().getValues();
  if (!values.length) {
    throw new Error('Outreach Control is empty');
  }
  const headers = values[0].map((header) => String(header || '').trim());
  let index = buildHeaderIndex_(headers);
  requireColumns_(index, ['Date']);
  if (!(OUTREACH_CONTROL_PROSPECTS_START_ROW in index)) {
    const nextCol = headers.length + 1;
    sheet.getRange(1, nextCol).setValue(OUTREACH_CONTROL_PROSPECTS_START_ROW);
    headers.push(OUTREACH_CONTROL_PROSPECTS_START_ROW);
    index = buildHeaderIndex_(headers);
  }
  const today = workflowDateKey_();
  const matches = [];
  for (let r = 1; r < values.length; r++) {
    const dateValue = values[r][index.Date];
    if (normalizeDateDisplay_(dateValue) === today || String(dateValue || '').trim() === today) {
      matches.push(r + 1);
    }
  }
  if (!matches.length) {
    throw new Error(`No Outreach Control row found for ${today}`);
  }
  if (matches.length > 1) {
    throw new Error(`Multiple Outreach Control rows found for ${today}`);
  }
  sheet.getRange(matches[0], index[OUTREACH_CONTROL_PROSPECTS_START_ROW] + 1).setValue(String(startRow));
  return {
    ok: true,
    rowNumber: matches[0],
    field: OUTREACH_CONTROL_PROSPECTS_START_ROW,
    value: String(startRow),
  };
}

function buildProspectRow_(finalRow, finalIndex, prospectsHeaders) {
  const bridgedAt = new Date().toISOString();
  const category = cell_(finalRow, finalIndex, 'Category');
  const p1LinkedIn = cell_(finalRow, finalIndex, 'P1 LinkedIn');
  const p2LinkedIn = cell_(finalRow, finalIndex, 'P2 LinkedIn');
  const mapped = {
    ID: cell_(finalRow, finalIndex, 'ID'),
    Company: cell_(finalRow, finalIndex, 'Company'),
    Website: normalizeUrl_(cell_(finalRow, finalIndex, 'Website')),
    'Company LinkedIn': normalizeUrl_(cell_(finalRow, finalIndex, 'Company LinkedIn')),
    'Emp Count': cell_(finalRow, finalIndex, 'Emp Count'),
    'Source Tab': '',
    'P1 Name': cell_(finalRow, finalIndex, 'P1 Name'),
    'P1 Title': cell_(finalRow, finalIndex, 'P1 Title'),
    'P1 LinkedIn': normalizeUrl_(p1LinkedIn),
    'P1 Email': cell_(finalRow, finalIndex, 'P1 Email'),
    'P1 Activity': cell_(finalRow, finalIndex, 'P1 Activity'),
    'P2 Name': cell_(finalRow, finalIndex, 'P2 Name'),
    'P2 Title': cell_(finalRow, finalIndex, 'P2 Title'),
    'P2 LinkedIn': normalizeUrl_(p2LinkedIn),
    'P2 Email': cell_(finalRow, finalIndex, 'P2 Email'),
    'P2 Activity': cell_(finalRow, finalIndex, 'P2 Activity'),
    'Engaged Person': pickEngagedPerson_(finalRow, finalIndex),
    'Touch Method': '',
    'Outreach Status': '',
    Outcome: '',
    'Date Queued': '',
    Notes: `source=final_bridge; category=${category}; bridged_at=${bridgedAt}`,
  };
  return prospectsHeaders.map((header) => mapped[header] || '');
}

function pickEngagedPerson_(row, index) {
  const use = cell_(row, index, 'Use').toLowerCase();
  if (use.indexOf('person 2') !== -1 || use === 'p2' || use === '2') {
    return 'Person 2';
  }
  if (cell_(row, index, 'P1 LinkedIn')) {
    return 'Person 1';
  }
  if (cell_(row, index, 'P2 LinkedIn')) {
    return 'Person 2';
  }
  return 'Person 1';
}

function findFinalRowById_(finalSheet, finalIndex, id) {
  if (!id || !('ID' in finalIndex)) {
    return 0;
  }
  const rowCount = Math.max(0, finalSheet.getLastRow() - 1);
  if (rowCount < 1) {
    return 0;
  }
  const values = finalSheet.getRange(2, finalIndex.ID + 1, rowCount, 1).getDisplayValues();
  for (let i = 0; i < values.length; i++) {
    if (String(values[i][0] || '').trim() === id) {
      return i + 2;
    }
  }
  return 0;
}

function rankLeadForFinal_(lead, activities) {
  let p1 = {
    name: lead.p1.name,
    title: lead.p1.title,
    linkedin: lead.p1.linkedin,
    email: lead.p1.email,
    activity: activities.p1Activity,
  };
  let p2 = {
    name: lead.p2.name,
    title: lead.p2.title,
    linkedin: lead.p2.linkedin,
    email: lead.p2.email,
    activity: activities.p2Activity,
  };

  if (shouldSwap_(p1.activity, p2.activity)) {
    const temp = p1;
    p1 = p2;
    p2 = temp;
  }

  const hasAnyActivity = String(p1.activity || '').trim() !== '' || String(p2.activity || '').trim() !== '';
  const row = {
    ID: lead.id,
    Company: lead.company,
    Website: lead.website,
    'Company LinkedIn': lead.companyLinkedIn,
    'Emp Count': lead.empCount,
    'Source Tab': lead.sourceTab,
    Use: hasAnyActivity ? lead.use : '',
    'P1 Name': p1.name,
    'P1 Title': p1.title,
    'P1 LinkedIn': p1.linkedin,
    'P1 Email': p1.email,
    'P1 Activity': p1.activity,
    'P2 Name': p2.name,
    'P2 Title': p2.title,
    'P2 LinkedIn': p2.linkedin,
    'P2 Email': p2.email,
    'P2 Activity': p2.activity,
  };
  row.Category = categoryForRow_(row);
  return row;
}

function shouldSwap_(p1Activity, p2Activity) {
  const p2Score = activityScore_(p2Activity);
  if (p2Score <= 0) {
    return false;
  }
  return p2Score > activityScore_(p1Activity);
}

function activityScore_(activity) {
  if (activity === 'Very active') {
    return 2;
  }
  if (activity === 'Active') {
    return 1;
  }
  return 0;
}

function categoryForRow_(row) {
  const linkedins = [row['P1 LinkedIn'], row['P2 LinkedIn']];
  const linkedinCount = linkedins.filter((url) => isLinkedInProfile_(url)).length;
  const activities = [row['P1 Activity'], row['P2 Activity']];
  const hasAnyActivity = activities.some((activity) => String(activity || '').trim() !== '');
  if (!hasAnyActivity) {
    return '';
  }
  if (linkedinCount >= 2) {
    if (activities.indexOf('Very active') !== -1) {
      return 'Hyper';
    }
    if (activities.indexOf('Active') !== -1) {
      return 'High';
    }
    return 'Low';
  }
  if (linkedinCount === 1) {
    if (activities.indexOf('Very active') !== -1) {
      return 'Alpha-medium';
    }
    if (activities.indexOf('Active') !== -1) {
      return 'Medium';
    }
    return 'Low';
  }
  return '';
}

function isLinkedInProfile_(url) {
  return /^https?:\/\/([^/]+\.)?linkedin\.com\/in\/[^/?#]+\/?/i.test(String(url || '').trim());
}

function sortFinal_(finalSheet, finalHeaders) {
  const lastRow = finalSheet.getLastRow();
  const lastColumn = finalHeaders.length;
  if (lastRow < 3) {
    return;
  }
  const index = buildHeaderIndex_(finalHeaders);
  const values = finalSheet.getRange(2, 1, lastRow - 1, lastColumn).getDisplayValues();
  const rows = values.filter((row) => {
    return String(row[index.ID] || '').trim() || String(row[index.Company] || '').trim();
  });
  rows.sort((a, b) => {
    const categoryA = categoryPriority_(a[index.Category]);
    const categoryB = categoryPriority_(b[index.Category]);
    if (categoryA !== categoryB) {
      return categoryA - categoryB;
    }
    const p2A = isLinkedInProfile_(a[index['P2 LinkedIn']]) ? 0 : 1;
    const p2B = isLinkedInProfile_(b[index['P2 LinkedIn']]) ? 0 : 1;
    if (p2A !== p2B) {
      return p2A - p2B;
    }
    return String(a[index.Company] || '').localeCompare(String(b[index.Company] || ''), undefined, { sensitivity: 'base' });
  });
  finalSheet.getRange(2, 1, lastRow - 1, lastColumn).clearContent();
  if (rows.length) {
    finalSheet.getRange(2, 1, rows.length, lastColumn).setValues(rows);
  }
}

function categoryPriority_(category) {
  const key = String(category || '').trim();
  return Object.prototype.hasOwnProperty.call(CATEGORY_PRIORITY, key) ? CATEGORY_PRIORITY[key] : 99;
}

function buildPayload_(leads, sourceSheet, finalSheet, meta) {
  return {
    ok: true,
    appTitle: 'Lead Activity',
    sourceTabName: CONFIG.sourceTabName,
    finalTabName: CONFIG.finalTabName,
    totalRows: Math.max(0, sourceSheet.getLastRow() - 1),
    finalRows: Math.max(0, finalSheet.getLastRow() - 1),
    categoryCounts: getFinalCategoryCounts_(finalSheet),
    workflowState: getWorkflowState_(),
    leads,
    activityValues: ACTIVITY_VALUES,
    meta: meta || {},
    generatedAt: new Date().toISOString(),
  };
}

function buildReviewPayload_(leads, groupDate, sheet) {
  return {
    ok: true,
    appTitle: 'Lead Review',
    reviewTabName: CONFIG.reviewTabName,
    groupDate,
    totalRows: Math.max(0, sheet.getLastRow() - 1),
    approvedCount: leads.filter((lead) => lead.approved).length,
    useValues: REVIEW_USE_VALUES,
    leads,
    generatedAt: new Date().toISOString(),
  };
}

function getFinalCategoryCounts_(finalSheet) {
  const counts = {
    Hyper: 0,
    High: 0,
    Medium: 0,
    Low: 0,
  };
  const values = finalSheet.getDataRange().getDisplayValues();
  if (values.length < 2) {
    return counts;
  }
  const index = buildHeaderIndex_(values[0].map((header) => String(header).trim()));
  if (!('Category' in index)) {
    return counts;
  }
  for (let r = 1; r < values.length; r++) {
    const category = String(values[r][index.Category] || '').trim();
    if (category === 'Alpha-medium' || category === 'Medium') {
      counts.Medium += 1;
    } else if (Object.prototype.hasOwnProperty.call(counts, category)) {
      counts[category] += 1;
    }
  }
  return counts;
}
