/**
 * Bound Google Apps Script API for the Daily Job Discovery spreadsheet.
 * Store API_TOKEN in Script Properties before deploying this as a Web App.
 */
const BOT_PROPERTIES = PropertiesService.getScriptProperties();
const WRITABLE_TABS = new Set(['Jobs', 'Companies', 'Runs', 'Review Queue']);
const LOCAL_ONLY_CONFIGURATION_TABS = ['Control', 'ATS Platforms', 'Roles & Vocabulary', 'Queries', 'Rules'];
const PRIORITY_VIEW_COLUMN_INDEXES = [1, 2, 3, 4, 5, 6, 9, 10, 18];
function priorityFormula(filters) {
  return '=IFERROR(CHOOSECOLS(FILTER(Jobs!A2:S,' + filters + '),1,2,3,4,5,6,9,10,18),"")';
}
const PRIORITY_VIEWS = [
  { name: 'High', formula: priorityFormula('Jobs!I2:I="verified",Jobs!J2:J="verified",Jobs!R2:R="active"') },
  { name: 'Medium', formula: priorityFormula('Jobs!I2:I="verified",Jobs!J2:J="conflicting",REGEXMATCH(Jobs!R2:R,"^(active|review)$")') },
  { name: 'Low I', formula: priorityFormula('Jobs!I2:I="senior_verified",Jobs!J2:J="verified",Jobs!R2:R="active"') },
  { name: 'Low II', formula: priorityFormula('Jobs!I2:I="senior_verified",Jobs!J2:J="conflicting",REGEXMATCH(Jobs!R2:R,"^(active|review)$")') },
  {
    name: 'Needs Review',
    formula: priorityFormula('REGEXMATCH(Jobs!R2:R,"^(active|review)$"),(((Jobs!I2:I="verified")*(Jobs!J2:J="verified")*(Jobs!R2:R="active"))+((Jobs!I2:I="verified")*(Jobs!J2:J="conflicting")*REGEXMATCH(Jobs!R2:R,"^(active|review)$"))+((Jobs!I2:I="senior_verified")*(Jobs!J2:J="verified")*(Jobs!R2:R="active"))+((Jobs!I2:I="senior_verified")*(Jobs!J2:J="conflicting")*REGEXMATCH(Jobs!R2:R,"^(active|review)$")))=0')
  },
  { name: 'Company Boards', formula: priorityFormula('Jobs!R2:R="company_board"') }
];

function jsonResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return jsonResponse({ ok: true, service: 'daily-job-discovery', message: 'Use authenticated POST requests for bot actions.' });
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Job Discovery')
    .addItem('Refresh priority views', 'refreshPriorityViews')
    .addToUi();
}

function refreshPriorityViews() {
  withDocumentLock(() => ensurePriorityViews());
  activeSpreadsheet().toast('Priority views refreshed.', 'Job Discovery', 4);
}

function doPost(event) {
  try {
    const request = JSON.parse(event.postData && event.postData.contents || '{}');
    if (!request.token || request.token !== BOT_PROPERTIES.getProperty('API_TOKEN')) return jsonResponse({ ok: false, error: 'Unauthorized' });
    if (request.action === 'bootstrap') return jsonResponse({ ok: true, ...withDocumentLock(() => bootstrap(request)) });
    if (request.action === 'getControl') return jsonResponse({ ok: true, control: getControl() });
    if (request.action === 'getConfiguration') return jsonResponse({ ok: true, configuration: getConfiguration() });
    if (request.action === 'upsert') return jsonResponse({ ok: true, ...withDocumentLock(() => upsert(request.tab, request.records || [])) });
    if (request.action === 'append') return jsonResponse({ ok: true, ...withDocumentLock(() => append(request.tab, request.rows || [])) });
    if (request.action === 'replace') return jsonResponse({ ok: true, ...withDocumentLock(() => replaceRows(request.tab, request.rows || [])) });
    if (request.action === 'retain') return jsonResponse({ ok: true, ...withDocumentLock(() => retainRows(request.tab, request.ids || [])) });
    if (request.action === 'syncProjection') return jsonResponse({ ok: true, ...withDocumentLock(() => syncProjection(request)) });
    return jsonResponse({ ok: false, error: 'Unsupported action' });
  } catch (error) {
    return jsonResponse({ ok: false, error: error.message || String(error) });
  }
}

function activeSpreadsheet() { return SpreadsheetApp.getActiveSpreadsheet(); }

function withDocumentLock(action) {
  const lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try { return action(); } finally { lock.releaseLock(); }
}

function bootstrap(request) {
  const spreadsheet = activeSpreadsheet();
  const created = [];
  const removedConfigurationTabs = [];
  const localFirst = Boolean(request.localFirst);
  if (localFirst) {
    LOCAL_ONLY_CONFIGURATION_TABS.forEach((name) => {
      const sheet = spreadsheet.getSheetByName(name);
      if (sheet) { spreadsheet.deleteSheet(sheet); removedConfigurationTabs.push(name); }
    });
  }
  const schemaNames = Object.keys(request.tabSchemas || {}).filter((name) => !localFirst || !LOCAL_ONLY_CONFIGURATION_TABS.includes(name));
  schemaNames.forEach((name) => {
    if (!spreadsheet.getSheetByName(name)) { spreadsheet.insertSheet(name); created.push(name); }
  });
  created.forEach((name) => {
    const sheet = spreadsheet.getSheetByName(name);
    const headers = request.tabSchemas[name];
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold').setBackground('#f1f3f4');
    sheet.setFrozenRows(1);
  });
  if (!localFirst) {
    if (created.includes('Control')) append('Control', request.defaultControl || []);
    else ensureRowsByKey('Control', request.defaultControl || []);
    if (created.includes('ATS Platforms')) append('ATS Platforms', request.platforms.map((item) => [item.name, item.siteTarget, item.enabled]));
    if (created.includes('Roles & Vocabulary')) append('Roles & Vocabulary', request.roles.flatMap((role) => [[role.name, 'junior', role.junior.join(' | ')], [role.name, 'unfiltered', role.unfiltered.join(' | ')]]));
    if (created.includes('Queries')) append('Queries', request.queries.map((item) => [item.id, item.platform, item.role, item.type, item.query, true]));
    const ruleRows = [['Rules version', '3', '3'], ['Junior signals', 'junior | jr | associate | entry level | new grad | graduate | I | 1 | early career', '2'], ['Senior signals', 'senior | staff | principal | lead | manager | director', '2'], ['Remote signals', 'remote | work from home | distributed | anywhere', '2'], ['Hybrid signals', 'hybrid', '3'], ['Onsite signals', 'no remote | on-site | onsite | in office', '3'], ['Non-remote signals', 'no remote | on-site | onsite | in office | hybrid only', '2'], ['Python signals', 'python', '2']];
    if (created.includes('Rules')) append('Rules', ruleRows);
    else ensureRowsByKey('Rules', ruleRows);
  }
  ensurePriorityViews();
  return { created, removedConfigurationTabs };
}

function ensurePriorityViews() {
  const spreadsheet = activeSpreadsheet();
  const jobs = requireSheet('Jobs');
  const jobsWidth = jobs.getLastColumn();
  const jobsHeaders = jobs.getRange(1, 1, 1, jobsWidth).getValues()[0];
  const headers = PRIORITY_VIEW_COLUMN_INDEXES.map((column) => jobsHeaders[column - 1]);
  const width = headers.length;
  PRIORITY_VIEWS.forEach((view) => {
    let sheet = spreadsheet.getSheetByName(view.name);
    if (!sheet) sheet = spreadsheet.insertSheet(view.name);
    const previousWidth = Math.max(sheet.getLastColumn(), width);
    sheet.getRange(1, 1, 1, previousWidth).clearContent();
    sheet.getRange(1, 1, 1, width).setValues([headers]).setFontWeight('bold').setBackground('#f1f3f4');
    const contentRows = Math.max(1, sheet.getMaxRows() - 1);
    sheet.getRange(2, 1, contentRows, previousWidth).clearContent();
    sheet.getRange(2, 1).setFormula(view.formula);
    sheet.setFrozenRows(1);
  });
}

function ensureRowsByKey(tab, rows) {
  if (!rows.length) return;
  const sheet = requireSheet(tab);
  const existing = new Set(sheet.getRange(1, 1, Math.max(sheet.getLastRow(), 1), 1).getDisplayValues().slice(1).map((row) => row[0]).filter(Boolean));
  append(tab, rows.filter((row) => row[0] && !existing.has(row[0])));
}

function getControl() {
  const sheet = requireSheet('Control');
  const values = sheet.getRange(1, 1, Math.max(sheet.getLastRow(), 1), 2).getDisplayValues();
  return Object.fromEntries(values.slice(1).filter((row) => row[0]).map((row) => [row[0], row[1] || '']));
}

function tabRows(tab, columns) {
  const sheet = requireSheet(tab);
  if (sheet.getLastRow() < 2) return [];
  return sheet.getRange(2, 1, sheet.getLastRow() - 1, columns).getDisplayValues().filter((row) => row.some((value) => value !== ''));
}

function getConfiguration() {
  return {
    platformRows: tabRows('ATS Platforms', 3),
    roleRows: tabRows('Roles & Vocabulary', 3),
    queryRows: tabRows('Queries', 6),
    ruleRows: tabRows('Rules', 3)
  };
}

function upsert(tab, records) {
  if (!WRITABLE_TABS.has(tab)) throw new Error('Writes are not allowed for tab: ' + tab);
  const sheet = requireSheet(tab);
  if (!records.length) return { inserted: 0, updated: 0 };
  const current = sheet.getDataRange().getValues();
  const headerWidth = current[0] ? current[0].length : 1;
  const width = Math.max(headerWidth, ...records.map((record) => record.values.length));
  const rows = current.slice(1).filter((row) => row.some((value) => value !== '')).map((row) => [...row, ...Array(Math.max(0, width - row.length)).fill('')]);
  const existing = new Map(rows.map((row, index) => [String(row[0]), index]).filter(([id]) => id));
  let inserted = 0;
  let updated = 0;
  records.forEach((record) => {
    const rowIndex = existing.get(String(record.id));
    if (rowIndex !== undefined) {
      record.values.forEach((value, column) => { rows[rowIndex][column] = value; });
      updated += 1;
    } else {
      const row = [...record.values, ...Array(Math.max(0, width - record.values.length)).fill('')];
      existing.set(String(record.id), rows.length); rows.push(row); inserted += 1;
    }
  });
  if (rows.length) sheet.getRange(2, 1, rows.length, width).setValues(rows);
  return { inserted, updated };
}

function append(tab, rows) {
  if (!WRITABLE_TABS.has(tab) && !['Control', 'ATS Platforms', 'Roles & Vocabulary', 'Queries', 'Rules'].includes(tab)) throw new Error('Writes are not allowed for tab: ' + tab);
  const sheet = requireSheet(tab);
  if (!rows.length) return { appended: 0 };
  sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
  return { appended: rows.length };
}

function replaceRows(tab, rows) {
  if (!WRITABLE_TABS.has(tab)) throw new Error('Writes are not allowed for tab: ' + tab);
  const sheet = requireSheet(tab);
  const lastRow = sheet.getLastRow();
  const width = Math.max(sheet.getLastColumn(), rows[0] ? rows[0].length : 1);
  if (lastRow > 1) sheet.getRange(2, 1, lastRow - 1, width).clearContent();
  if (rows.length) sheet.getRange(2, 1, rows.length, rows[0].length).setValues(rows);
  return { replaced: rows.length };
}

function retainRows(tab, ids) {
  if (!WRITABLE_TABS.has(tab)) throw new Error('Writes are not allowed for tab: ' + tab);
  const sheet = requireSheet(tab);
  const current = sheet.getDataRange().getValues();
  if (current.length < 2) return { retained: 0, removed: 0 };
  const allowed = new Set(ids.map(String));
  const rows = current.slice(1).filter((row) => row.some((value) => value !== '') && allowed.has(String(row[0])));
  const width = current[0].length;
  sheet.getRange(2, 1, current.length - 1, width).clearContent();
  if (rows.length) sheet.getRange(2, 1, rows.length, width).setValues(rows.map((row) => [...row, ...Array(Math.max(0, width - row.length)).fill('')].slice(0, width)));
  return { retained: rows.length, removed: current.length - 1 - rows.length };
}

function syncProjection(request) {
  retainRows('Jobs', request.jobIds || []);
  retainRows('Companies', request.companyIds || []);
  const jobs = upsert('Jobs', request.jobs || []);
  const companies = upsert('Companies', request.companies || []);
  const reviews = replaceRows('Review Queue', request.reviews || []);
  const runs = upsert('Runs', request.runs || []);
  ensurePriorityViews();
  return { jobs, companies, reviews, runs, priorityViews: PRIORITY_VIEWS.map((view) => view.name), version: 3 };
}

function requireSheet(tab) {
  const sheet = activeSpreadsheet().getSheetByName(tab);
  if (!sheet) throw new Error('Missing required tab: ' + tab);
  return sheet;
}
