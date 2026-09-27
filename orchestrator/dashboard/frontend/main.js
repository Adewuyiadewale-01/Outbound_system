const { app, BrowserWindow, ipcMain, screen, shell, Menu } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const net = require('net');
const { spawn, exec, spawnSync } = require('child_process');

let floatingWindow = null;
let drawerWindow = null;
let activeProcess = null;
let activeProcessMeta = null;
let activeProcessStarting = false;
let postEngagementProcess = null;

function isProjectRoot(candidate) {
  return Boolean(candidate)
    && fs.existsSync(path.join(candidate, 'state'))
    && fs.existsSync(path.join(candidate, 'scripts'))
    && fs.existsSync(path.join(candidate, 'orchestrator'));
}

function resolveProjectDir() {
  const packagedSiblingRoot = path.resolve(process.resourcesPath, '..', '..', '..', '..');
  const candidates = [
    process.env.OUTREACH_AUTOMATION_ROOT,
    app.isPackaged ? packagedSiblingRoot : path.resolve(__dirname, '..', '..', '..'),
    path.join(os.homedir(), 'codex-outreach-automation')
  ];
  const resolved = candidates.find(isProjectRoot);
  if (resolved) return resolved;
  // Preserve a useful diagnostic path rather than silently reading the app bundle.
  return path.resolve(process.env.OUTREACH_AUTOMATION_ROOT || (app.isPackaged ? packagedSiblingRoot : path.join(__dirname, '..', '..', '..')));
}

const PROJECT_DIR = resolveProjectDir();
const STATE_DIR = path.join(PROJECT_DIR, 'state');

function loadProjectEnv() {
  const envPath = path.join(PROJECT_DIR, '.env');
  try {
    fs.readFileSync(envPath, 'utf8').split(/\r?\n/).forEach(line => {
      const match = line.match(/^\s*([A-Z0-9_]+)=(.*)\s*$/);
      if (match && process.env[match[1]] === undefined) {
        process.env[match[1]] = match[2].replace(/^['"]|['"]$/g, '');
      }
    });
  } catch (error) {
    if (error.code !== 'ENOENT') console.warn(`Could not read ${envPath}: ${error.message}`);
  }
}

loadProjectEnv();

function isJobDiscoveryRoot(candidate) {
  return Boolean(candidate)
    && fs.existsSync(path.join(candidate, 'src', 'cli.mjs'))
    && fs.existsSync(path.join(candidate, 'config', 'runtime.json'))
    && fs.existsSync(path.join(candidate, 'package.json'));
}

function resolveJobDiscoveryDir() {
  const candidates = [
    process.env.DAILY_JOB_DISCOVERY_ROOT,
    path.join(os.homedir(), 'Documents', 'Automation Journey', 'daily-job-discovery')
  ];
  return candidates.find(isJobDiscoveryRoot) || candidates[0];
}

const JOB_DISCOVERY_DIR = resolveJobDiscoveryDir();
const JOB_DISCOVERY_CONFIG_PATH = path.join(JOB_DISCOVERY_DIR, 'config', 'runtime.json');
const JOB_DISCOVERY_LOCK_PATH = path.join(JOB_DISCOVERY_DIR, 'data', 'state.sqlite.lock');
const JOB_DISCOVERY_SCHEDULER_LABEL = 'com.fulltime-job.daily-job-discovery';

function getTodayString() {
  const date = new Date();
  const offset = date.getTimezoneOffset();
  const localDate = new Date(date.getTime() - (offset * 60 * 1000));
  return localDate.toISOString().split('T')[0];
}

const OBF_CONFIG_PATH = path.join(STATE_DIR, 'obf_orchestration_config.json');
const FOLLOWUP_CONFIG_PATH = path.join(STATE_DIR, 'followup_orchestration_config.json');
const LEAD_PREP_CONFIG_PATH = path.join(STATE_DIR, 'lead_prep_orchestration_config.json');
const LEAD_PREP_ARCHIVE_PATH = path.join(STATE_DIR, 'lead_exec_research', 'research_archive', 'index.json');
const LEAD_PREP_RUNS_DIR = path.join(STATE_DIR, 'lead_exec_research', 'runs');
const LEAD_PREP_COMPUTATIONS_DIR = path.join(STATE_DIR, 'lead_exec_research', 'computations');
const LEAD_REVIEW_CACHE_PATH = path.join(STATE_DIR, 'lead_exec_research', 'dashboard_cache.json');
const LEAD_RESEARCH_PROGRESS_PATH = path.join(STATE_DIR, 'lead_exec_research', 'manual_research_progress.json');
const ACTIVITY_SESSIONS_DIR = path.join(STATE_DIR, 'activity_sessions');
const WATCHER_SCRIPT = path.join(PROJECT_DIR, 'orchestrator', 'watcher', 'orchestration_watcher.py');
const CHROME_CDP_HOST = '127.0.0.1';
const CHROME_CDP_PORT = 18800;
const CHROME_LAUNCH_SCRIPT = path.join(PROJECT_DIR, 'scripts', 'launch-chrome.sh');
const CHROME_2_LAUNCH_SCRIPT = path.join(PROJECT_DIR, 'scripts', 'launch-chrome-2.sh');
let activityHistoryCache = { signature: '', rows: [] };
const PROCESS_APPROVED_FIRST_AUTOMATION_PATH = path.join(os.homedir(), '.codex', 'automations', 'process-approved-leads', 'automation.toml');
const PROCESS_APPROVED_FALLBACK_AUTOMATION_PATH = path.join(os.homedir(), '.codex', 'automations', 'process-approved-leads-23-00', 'automation.toml');
const OBF_CONFIG_DEFAULTS = {
  enabled: false,
  prep_time: '08:25',
  exec_time: '08:30',
  max_sends: 30,
  daily_volume: 30,
  prospects_start_row: null,
  timezone: 'Africa/Lagos'
};
const LEAD_PREP_CONFIG_DEFAULTS = {
  autonomous_prep_enabled: false,
  prep_time: '14:00',
  first_review_deadline: '18:00',
  fallback_review_deadline: '22:00',
  base_volume: 60,
  processing_batch_size: 30,
  approval_gate_enabled: false,
  overlap_scan_mode: 'auto',
  fresh_volume_top_up_mode: 'auto',
  dashboard_date_override: ''
};

function readJsonFile(filePath, fallback = {}) {
  try {
    return fs.existsSync(filePath) ? JSON.parse(fs.readFileSync(filePath, 'utf8')) : fallback;
  } catch (error) {
    return fallback;
  }
}

function jobDiscoveryAvailable() {
  return isJobDiscoveryRoot(JOB_DISCOVERY_DIR);
}

function jobDiscoverySchedulerEnabled() {
  if (process.platform !== 'darwin') return false;
  const result = spawnSync('launchctl', ['print', `gui/${process.getuid()}/${JOB_DISCOVERY_SCHEDULER_LABEL}`], {
    stdio: 'ignore'
  });
  return result.status === 0;
}

function runJobDiscoveryNode(args, { timeoutMs = 30_000 } = {}) {
  return new Promise(resolve => {
    if (!jobDiscoveryAvailable()) {
      resolve({ ok: false, code: -1, stdout: '', stderr: `Daily Job Discovery was not found at ${JOB_DISCOVERY_DIR}.` });
      return;
    }
    const child = spawn('node', args, {
      cwd: JOB_DISCOVERY_DIR,
      env: { ...process.env }
    });
    let stdout = '';
    let stderr = '';
    const timeout = setTimeout(() => child.kill('SIGTERM'), timeoutMs);
    child.stdout.on('data', data => { stdout += data.toString(); });
    child.stderr.on('data', data => { stderr += data.toString(); });
    child.on('error', error => {
      clearTimeout(timeout);
      resolve({ ok: false, code: -1, stdout, stderr: `${stderr}${error.message}` });
    });
    child.on('close', code => {
      clearTimeout(timeout);
      resolve({ ok: code === 0, code, stdout, stderr });
    });
  });
}

async function readJobDiscoveryDashboard() {
  const config = readJsonFile(JOB_DISCOVERY_CONFIG_PATH, {});
  const lock = readJsonFile(JOB_DISCOVERY_LOCK_PATH, null);
  const schedulerEnabled = jobDiscoverySchedulerEnabled();
  const activeHere = activeProcessMeta?.workflow === 'job_discovery';
  if (!jobDiscoveryAvailable()) {
    return {
      available: false,
      root: JOB_DISCOVERY_DIR,
      error: `Daily Job Discovery was not found at ${JOB_DISCOVERY_DIR}. Set DAILY_JOB_DISCOVERY_ROOT to use a different location.`,
      config,
      scheduler_enabled: false,
      is_running: false,
      status: {}
    };
  }
  const result = await runJobDiscoveryNode(['src/cli.mjs', 'status']);
  let status = {};
  let error = result.ok ? '' : (result.stderr || result.stdout || 'Status command failed.').trim();
  try {
    status = JSON.parse(result.stdout);
  } catch (parseError) {
    if (!error) error = 'Daily Job Discovery returned an unreadable status response.';
  }
  return {
    available: true,
    root: JOB_DISCOVERY_DIR,
    config,
    scheduler_enabled: schedulerEnabled,
    is_running: activeHere || Boolean(status.activeRunId) || Boolean(lock?.runId),
    active_action: activeHere ? activeProcessMeta.action : (status.activeRunId ? 'scheduled run' : ''),
    lock,
    status,
    error,
    last_updated: new Date().toISOString()
  };
}

function saveJobDiscoverySettings(requested = {}) {
  if (!jobDiscoveryAvailable()) return { ok: false, error: `Daily Job Discovery was not found at ${JOB_DISCOVERY_DIR}.` };
  const current = readJsonFile(JOB_DISCOVERY_CONFIG_PATH, {});
  const dailyRunTime = String(requested.dailyRunTime || current.dailyRunTime || '').trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(dailyRunTime)) {
    return { ok: false, error: 'Daily run time must use 24-hour HH:MM format.' };
  }
  const boundedInteger = (value, fallback, maximum) => Math.max(1, Math.min(maximum, Math.floor(Number(value) || fallback)));
  const next = {
    ...current,
    automationEnabled: Boolean(requested.automationEnabled),
    dailyRunTime,
    maxQueriesPerRun: boundedInteger(requested.maxQueriesPerRun, current.maxQueriesPerRun || 180, 500),
    maxListingsPerRun: boundedInteger(requested.maxListingsPerRun, current.maxListingsPerRun || 180, 1000)
  };
  writeJsonFile(JOB_DISCOVERY_CONFIG_PATH, next);
  return { ok: true, config: next };
}

function readJsonLines(filePath) {
  if (!fs.existsSync(filePath)) return [];
  try {
    return fs.readFileSync(filePath, 'utf8')
      .split('\n')
      .filter(line => line.trim())
      .map(line => JSON.parse(line));
  } catch (error) {
    return [];
  }
}

function readAutomationStatus(filePath) {
  try {
    const match = fs.readFileSync(filePath, 'utf8').match(/^status\s*=\s*"([^"]+)"/m);
    return match ? match[1] : 'UNKNOWN';
  } catch (error) {
    return 'UNKNOWN';
  }
}

function readObfConfig() {
  return { ...OBF_CONFIG_DEFAULTS, ...readJsonFile(OBF_CONFIG_PATH, {}) };
}

function readFollowupConfig() {
  return readJsonFile(FOLLOWUP_CONFIG_PATH, { enabled: false });
}

function readLeadPrepConfig() {
  const stored = readJsonFile(LEAD_PREP_CONFIG_PATH, {});
  if (!stored.overlap_scan_mode && stored.reconciliation_mode) {
    stored.overlap_scan_mode = stored.reconciliation_mode;
  }
  if (stored.review_deadline && !stored.fallback_review_deadline) {
    stored.fallback_review_deadline = stored.review_deadline;
  }
  return { ...LEAD_PREP_CONFIG_DEFAULTS, ...stored };
}

function latestJsonFile(directory) {
  if (!fs.existsSync(directory)) return null;
  const files = fs.readdirSync(directory)
    .filter(name => name.endsWith('.json'))
    .sort()
    .reverse();
  return files.length ? path.join(directory, files[0]) : null;
}

function latestMatchingLeadComputation(reviewLeads) {
  if (!fs.existsSync(LEAD_PREP_COMPUTATIONS_DIR) || !reviewLeads.length) return { file: '', data: {} };
  const leadIds = new Set(reviewLeads.map(lead => String(lead.run_id || '')).filter(Boolean));
  const files = fs.readdirSync(LEAD_PREP_COMPUTATIONS_DIR)
    .filter(name => name.endsWith('.json'))
    .sort()
    .reverse();
  for (const name of files) {
    const file = path.join(LEAD_PREP_COMPUTATIONS_DIR, name);
    const data = readJsonFile(file, {});
    if ((data.leads || []).some(lead => leadIds.has(String(lead.lead_id || '')))) {
      return { file, data };
    }
  }
  return { file: '', data: {} };
}

function manualComputationPath(review) {
  const day = String(review.date || 'undated').replace(/[^0-9-]/g, '');
  const groupRow = Number(review.group_row || 0);
  return path.join(LEAD_PREP_COMPUTATIONS_DIR, `manual_${day}_${groupRow}_computation.json`);
}

function validLinkedinProfile(value) {
  return /^https?:\/\/([a-z]{2,3}\.)?linkedin\.com\/in\/[^?#\s]+(?:[?#].*)?$/i
    .test(String(value || '').trim());
}

function manualLeadReady(lead) {
  const first = (lead.executives || [])[0] || {};
  return Boolean(
    String(first.name || '').trim()
    && String(first.title || '').trim()
    && validLinkedinProfile(first.linkedin_url)
  );
}

function normalizeManualExecutives(raw) {
  const executives = [];
  (Array.isArray(raw) ? raw : []).slice(0, 3).forEach(item => {
    const person = {
      name: String(item?.name || '').trim().slice(0, 200),
      title: String(item?.title || '').trim().slice(0, 300),
      linkedin_url: String(item?.linkedin_url || '').trim().slice(0, 500),
      email: String(item?.email || '').trim().slice(0, 300),
      research_source: 'manual_dashboard',
      linkedin_source: 'manual_dashboard',
      needs_linkedin_search: false,
      reconciliation_notes: []
    };
    if (person.linkedin_url && !validLinkedinProfile(person.linkedin_url)) {
      throw new Error('LinkedIn URLs must be profile links using linkedin.com/in/.');
    }
    executives.push(person);
  });
  while (executives.length) {
    const person = executives[executives.length - 1];
    if (person.name || person.title || person.linkedin_url || person.email) break;
    executives.pop();
  }
  return executives;
}

function createManualComputation(dashboard) {
  const review = dashboard.review || {};
  const processing = dashboard.processing || {};
  const file = manualComputationPath(review);
  const existing = readJsonFile(file, {});
  if (Object.keys(existing).length) return { file, computation: existing };
  const sourceRun = readJsonFile(dashboard.run?.file || '', {});
  const sourceById = new Map(
    (sourceRun.leads || []).map(item => [String(item.id || ''), item])
  );
  const leads = (processing.leads || []).map(lead => {
    const source = sourceById.get(String(lead.run_id || '')) || {};
    const company = source.company || {};
    return {
      lead_id: lead.run_id || '',
      company: lead.company || '',
      website: lead.website || '',
      company_linkedin: company.linkedin || '',
      emp_count: company.employee_count ?? lead.employee_count ?? '',
      source_tab: source.source_tab || '',
      use: lead.use || '',
      primary_lane: lead.primary_lane || '',
      prep_wave: lead.prep_wave || 'Base',
      source_rows: source.source_rows || {},
      employees_from_sheet: source.employees_from_sheet || [],
      executives: (lead.executives || []).slice(0, 3),
      search_tasks: [],
      search_results: [],
      destination_row: {},
      status: manualLeadReady(lead) ? 'manual_ready' : 'manual_draft',
      notes: []
    };
  });
  if (!leads.length) throw new Error('No reviewed leads are available for manual research.');
  const now = new Date().toISOString();
  const computation = {
    computation_id: path.basename(file, '.json'),
    created_at: now,
    updated_at: now,
    mode: 'manual',
    status: leads.every(manualLeadReady) ? 'manual_research_ready' : 'manual_research_in_progress',
    source_run_file: dashboard.run?.file || '',
    review_date: review.date || '',
    review_group_row: review.group_row,
    lead_count: leads.length,
    leads,
    search_tasks: [],
    search_tasks_remaining: 0,
    writes: [],
    errors: []
  };
  writeJsonFile(file, computation);
  return { file, computation };
}

function processingFinalReport(computation, computationFile = '') {
  if (!computation || !Object.keys(computation).length) {
    return {
      available: false,
      terminal: false,
      outcome: 'waiting',
      outcome_label: 'Waiting for computation',
      tone: 'neutral',
      mode: 'unknown',
      mode_label: 'Not started',
      summary: 'No persisted Process Approved Leads computation matches this review group yet.',
      counts: {},
      write: {},
      issues: [],
      last_updated: null,
      computation_file: computationFile
    };
  }
  const leads = computation.leads || [];
  const tasks = computation.search_tasks || [];
  const batches = computation.search_result_batches || [];
  const writes = computation.writes || [];
  const latestBatch = batches[batches.length - 1] || {};
  // A post-run dry-run may be saved after a successful publication. Surface the
  // actual Sheet result in the dashboard, falling back to the dry-run only
  // when it is the only recorded write attempt.
  const latestWrite = [...writes].reverse().find(write => !write?.dry_run)
    || writes[writes.length - 1]
    || {};
  const skipped = latestWrite.skipped_unresolved || [];
  const batchResults = latestBatch.results || [];
  const errors = computation.errors || [];
  const researchCompleted = leads.filter(lead => (lead.executives || []).length).length;
  const personCoverage = [0, 1, 2].map(index => leads.filter(lead => {
    const person = (lead.executives || [])[index] || {};
    return Boolean(String(person.name || '').trim());
  }).length);
  const rowsReady = leads.filter(lead => {
    const first = (lead.executives || [])[0] || {};
    return String(lead.lead_id || '').trim()
      && String(lead.company || '').trim()
      && String(lead.website || '').trim()
      && String(first.name || '').trim()
      && validLinkedinProfile(first.linkedin_url);
  }).length;
  const pendingTasks = tasks.filter(task => String(task.status || 'pending') === 'pending').length;
  const selectedTasks = tasks.filter(task => String(task.status || '') === 'selected').length;
  const searchErrors = batchResults.filter(result => String(result.status || '') === 'error').length;
  const needsReview = batchResults.filter(result => String(result.status || '') === 'needs_review').length;
  const retryCount = tasks.reduce((total, task) => total + Math.max(
    Number(task.retry_count || 0),
    Math.max(0, Number(task.attempts || 1) - 1)
  ), 0);
  const conflictCount = leads.filter(lead => String(lead.status || '') === 'archive_conflict').length;
  const rowsWritten = Number(latestWrite.rows_written || 0);
  const skippedCount = Number(latestWrite.skipped_unresolved_count ?? skipped.length);
  const rawStatus = String(computation.status || 'unknown');
  const loweredStatus = rawStatus.toLowerCase();
  const errorCount = errors.length + searchErrors;
  let outcome = 'in_progress';
  let outcomeLabel = 'Persisted state available';
  let tone = 'active';
  let terminal = false;
  if (loweredStatus.includes('fail') || loweredStatus.includes('error')) {
    [outcome, outcomeLabel, tone, terminal] = ['failed', 'Failed', 'danger', true];
  } else if (rowsWritten || ['written', 'write_partial'].includes(loweredStatus)) {
    [outcome, outcomeLabel, tone, terminal] = skippedCount || rowsWritten < leads.length
      ? ['partial', 'Completed with unresolved rows', 'warning', true]
      : ['completed', 'Completed and written', 'success', true];
  } else if (loweredStatus.includes('archive') && (loweredStatus.includes('unreviewed') || loweredStatus === 'archived')) {
    [outcome, outcomeLabel, tone, terminal] = ['archived', 'Archived', 'neutral', true];
  } else if (errorCount) {
    [outcome, outcomeLabel, tone] = ['attention', 'Attention required', 'warning'];
  }
  const mode = String(computation.mode || '') === 'manual' ? 'manual' : 'codex';
  const issues = skipped.map(item => ({
    type: 'unresolved',
    tone: 'warning',
    lead_id: String(item.lead_id || ''),
    company: String(item.company || ''),
    message: (item.reasons || []).map(reason => String(reason).replaceAll('_', ' ')).join(', ')
      || 'Row was skipped as unresolved.'
  }));
  batchResults.forEach(result => {
    if (!['error', 'needs_review'].includes(String(result.status || ''))) return;
    issues.push({
      type: 'search',
      tone: String(result.status || '') === 'error' ? 'danger' : 'warning',
      lead_id: String(result.lead_id || ''),
      company: String(result.company || ''),
      message: String(result.error || result.message || 'LinkedIn search needs review.')
    });
  });
  leads.forEach(lead => {
    if (String(lead.status || '') !== 'archive_conflict') return;
    issues.push({
      type: 'archive_conflict',
      tone: 'warning',
      lead_id: String(lead.lead_id || ''),
      company: String(lead.company || ''),
      message: 'Archive identity conflict requires reconciliation.'
    });
  });
  errors.forEach(error => {
    const message = typeof error === 'object'
      ? (error.error || error.message || error.stderr || JSON.stringify(error))
      : String(error);
    issues.push({ type: 'error', tone: 'danger', lead_id: '', company: '', message: String(message) });
  });
  const destination = String(latestWrite.destination_tab || '');
  const published = latestWrite.queue_batch?.prefinal_publish || {};
  const writeTimestamp = published.published_at || latestWrite.created_at || null;
  let summary = `${researchCompleted} of ${leads.length} leads have persisted research; ${pendingTasks} search ${pendingTasks === 1 ? 'task remains' : 'tasks remain'}.`;
  if (outcome === 'completed') {
    summary = `${rowsWritten} of ${leads.length} leads were written to ${destination || 'Pre-final'}.`;
  } else if (outcome === 'partial') {
    summary = `${rowsWritten} of ${leads.length} leads were written; ${skippedCount} unresolved ${skippedCount === 1 ? 'row was' : 'rows were'} skipped.`;
  } else if (outcome === 'archived') {
    summary = `${leads.length} leads were retained in the research archive instead of being published.`;
  } else if (outcome === 'failed') {
    summary = 'The persisted computation ended in a failure state. Review the reported issues before retrying.';
  }
  return {
    available: true,
    terminal,
    outcome,
    outcome_label: outcomeLabel,
    tone,
    raw_status: rawStatus,
    mode,
    mode_label: mode === 'manual' ? 'Manual dashboard' : 'Codex orchestration',
    summary,
    counts: {
      lead_count: leads.length,
      research_completed: researchCompleted,
      research_pending: Math.max(0, leads.length - researchCompleted),
      p1_count: personCoverage[0],
      p2_count: personCoverage[1],
      p3_count: personCoverage[2],
      rows_ready: Math.max(rowsReady, rowsWritten),
      rows_written: rowsWritten,
      rows_skipped: skippedCount,
      search_total: tasks.length,
      search_completed: Math.max(selectedTasks, Number(latestBatch.completed_tasks || 0)),
      search_pending: Object.prototype.hasOwnProperty.call(computation, 'search_tasks_remaining')
        ? Number(computation.search_tasks_remaining || 0)
        : pendingTasks,
      search_needs_review: needsReview,
      search_errors: searchErrors,
      archive_conflicts: conflictCount,
      retries: retryCount,
      captcha_events: Number(latestBatch.captcha_events || 0),
      errors: errorCount
    },
    write: {
      attempted: Boolean(writes.length),
      destination,
      created_at: writeTimestamp,
      verified: published.verified,
      status: String(published.status || ''),
      queue_fingerprint: String(latestWrite.queue_batch?.fingerprint || '')
    },
    issues,
    last_updated: writeTimestamp || latestBatch.created_at || computation.updated_at || computation.created_at || null,
    computation_file: computationFile
  };
}

function latestMatchingLeadRun(reviewLeads) {
  if (!fs.existsSync(LEAD_PREP_RUNS_DIR) || !reviewLeads.length) return { file: '', data: {} };
  const expectedIds = new Set(reviewLeads.map(lead => String(lead.run_id || '')).filter(Boolean));
  const files = fs.readdirSync(LEAD_PREP_RUNS_DIR).filter(name => name.endsWith('.json')).sort().reverse();
  let best = { file: '', data: {}, overlap: 0 };
  for (const name of files) {
    const file = path.join(LEAD_PREP_RUNS_DIR, name);
    const data = readJsonFile(file, {});
    const runIds = new Set((data.leads || []).map(lead => String(lead.id || '')).filter(Boolean));
    const overlap = [...expectedIds].filter(id => runIds.has(id)).length;
    if (runIds.size === expectedIds.size && overlap === expectedIds.size) return { file, data };
    if (overlap > best.overlap) best = { file, data, overlap };
  }
  return { file: best.file, data: best.data };
}

function runMatchesDay(run, today) {
  if (String(run.created_at || '').startsWith(today)) return true;
  const reviewDate = run.review?.write?.date || run.review_write?.date || '';
  const match = String(reviewDate).match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  return Boolean(match && `${match[3]}-${match[1].padStart(2, '0')}-${match[2].padStart(2, '0')}` === today);
}

function computationDestinationRow(lead) {
  const row = { ...(lead.destination_row || {}) };
  (lead.executives || []).slice(0, 3).forEach((person, index) => {
    const slot = index + 1;
    row[`P${slot} Name`] ??= person.name || '';
    row[`P${slot} Title`] ??= person.title || '';
    row[`P${slot} LinkedIn`] ??= person.linkedin_url || '';
    row[`P${slot} Email`] ??= person.email || '';
  });
  return row;
}

function readActivityDashboard(today) {
  const file = path.join(ACTIVITY_SESSIONS_DIR, `${today}.json`);
  const session = readJsonFile(file, {});
  const recorded = session.targets || {};
  const recordedByKey = Array.isArray(recorded)
    ? new Map(recorded.map(item => [String(item.key || ''), item]))
    : new Map(Object.entries(recorded));
  const targets = (session.prepared_targets || []).map(item => {
    const live = recordedByKey.get(String(item.key || '')) || {};
    return {
      ...item,
      activity_value: live.activity_value || '',
      status: live.status || 'prepared',
      recorded_at: live.recorded_at || null
    };
  });
  const recordedCount = targets.filter(target => target.status === 'recorded').length;
  const activeSignals = targets.filter(target => ['active', 'very active'].includes(String(target.activity_value || '').trim().toLowerCase())).length;
  const checkpoints = readJsonFile(path.join(STATE_DIR, 'orchestration_watcher.json'), {})?.workflows?.activity?.days?.[today]?.checkpoints || {};
  const checkpointRows = Object.values(checkpoints).filter(item => item?.started_at);
  const activeRows = checkpointRows.filter(item => ['running', 'waiting'].includes(item.status));
  const currentCheckpoint = [...(activeRows.length ? activeRows : checkpointRows)]
    .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0] || {};
  const runStartedAt = String(currentCheckpoint.started_at || '');
  const currentRows = runStartedAt ? targets.filter(target => String(target.recorded_at || '') >= runStartedAt) : [];
  const priorRecords = runStartedAt ? targets.filter(target => target.recorded_at && String(target.recorded_at) < runStartedAt).length : 0;
  const currentTotal = runStartedAt ? Math.max(0, targets.length - priorRecords) : targets.length;
  return {
    exists: Boolean(Object.keys(session).length),
    file: fs.existsSync(file) ? file : '',
    status: session.status || 'not_prepared',
    target_count: targets.length,
    lead_ids: [...new Set(targets.map(item => String(item.lead_id || '')).filter(Boolean))],
    targets,
    prepared_at: session.prepared_at || null,
    updated_at: session.updated_at || session.completed_at || null,
    failures: session.failures || [],
    stats: {
      profiles_prepared: targets.length,
      profiles_recorded: recordedCount,
      profiles_pending: Math.max(0, targets.length - recordedCount),
      unique_leads: new Set(targets.map(item => String(item.lead_id || '')).filter(Boolean)).size,
      active_signals: activeSignals,
      bridged_rows: Number(session.bridged_rows_count || 0),
      failures: (session.failures || []).length
    },
    current_run: {
      started_at: runStartedAt || null,
      status: currentCheckpoint.status || 'idle',
      profiles_total: currentTotal,
      profiles_completed: currentRows.length,
      profiles_pending: Math.max(0, currentTotal - currentRows.length),
      prior_records: priorRecords,
      active_signals: currentRows.filter(target => ['active', 'very active'].includes(String(target.activity_value || '').trim().toLowerCase())).length
    },
    quarantine_issues: readActivityQuarantineIssues(),
    history: readActivityHistory()
  };
}

function readActivityQuarantineIssues() {
  const queueDir = path.join(STATE_DIR, 'prefinal_queue');
  if (!fs.existsSync(queueDir)) return [];
  const issues = [];
  for (const fileName of fs.readdirSync(queueDir).filter(name => name.endsWith('.json')).sort()) {
    const queue = readJsonFile(path.join(queueDir, fileName), {});
    if (['prospects_bridged', 'final_bridged'].includes(queue.status)) continue;
    const activityIssues = queue.activity_issues || {};
    const rowsById = new Map((queue.rows || []).map(row => [String(row.ID || '').trim(), row]));
    for (const [key, issue] of Object.entries(activityIssues).sort(([a], [b]) => a.localeCompare(b))) {
      const [leadId, person = ''] = String(key).split(':');
      const row = rowsById.get(String(leadId || '').trim()) || {};
      issues.push({
        key,
        queue_fingerprint: queue.fingerprint || fileName.replace(/\.json$/, ''),
        lead_id: leadId || '',
        company: row.Company || '',
        person,
        name: row[`${person} Name`] || '',
        title: row[`${person} Title`] || '',
        profile_url: issue.profile_url || '',
        reason: issue.terminal_reason || issue.reason || '',
        recorded_at: issue.recorded_at || ''
      });
    }
  }
  return issues;
}

function readActivityHistory() {
  if (!fs.existsSync(ACTIVITY_SESSIONS_DIR)) return [];
  const files = fs.readdirSync(ACTIVITY_SESSIONS_DIR)
    .filter(name => name.endsWith('.json'))
    .sort()
    .reverse()
    .map(name => path.join(ACTIVITY_SESSIONS_DIR, name));
  const signature = files.map(file => `${file}:${fs.statSync(file).mtimeMs}`).join('|');
  if (signature === activityHistoryCache.signature) return activityHistoryCache.rows;
  const rows = files.map(file => {
    const session = readJsonFile(file, {});
    const prepared = session.prepared_targets || [];
    const recorded = Array.isArray(session.targets)
      ? session.targets
      : Object.values(session.targets || {});
    const recordedCount = recorded.filter(item => item.status === 'recorded').length;
    const activeSignals = recorded.filter(item => ['active', 'very active'].includes(String(item.activity_value || '').trim().toLowerCase())).length;
    return {
      date: String(session.date || path.basename(file, '.json')),
      status: String(session.status || 'not_prepared'),
      profiles_prepared: prepared.length,
      profiles_recorded: recordedCount,
      profiles_pending: Math.max(0, prepared.length - recordedCount),
      unique_leads: new Set(prepared.map(item => String(item.lead_id || '')).filter(Boolean)).size,
      active_signals: activeSignals,
      bridged_rows: Number(session.bridged_rows_count || 0),
      failures: (session.failures || []).length,
      updated_at: session.updated_at || session.completed_at || session.prepared_at || null
    };
  });
  activityHistoryCache = { signature, rows };
  return rows;
}

function readLeadPrepDashboard() {
  const config = readLeadPrepConfig();
  const archive = readJsonFile(LEAD_PREP_ARCHIVE_PATH, { entries: {}, updated_at: '' });
  const entries = Object.values(archive.entries || {});
  const available = entries.filter(entry => (entry.status || 'available') === 'available').length;
  const consumed = entries.filter(entry => entry.status === 'consumed').length;
  let runPath = latestJsonFile(LEAD_PREP_RUNS_DIR);
  let run = runPath ? readJsonFile(runPath, {}) : {};
  let overlapScan = run.overlap_scan || run.archive_reconciliation || {};
  const reviewCache = readJsonFile(LEAD_REVIEW_CACHE_PATH, {});
  const progressState = readJsonFile(LEAD_RESEARCH_PROGRESS_PATH, { days: {} });
  const today = config.dashboard_date_override || getTodayString();
  const cachedToday = reviewCache.today?.date === today
    ? reviewCache.today
    : {
        date: today,
        review_complete: false,
        prepared_count: 0,
        approved_count: 0,
        case_study_worthy_count: 0,
        archive_match_count: 0,
        conflict_count: 0,
        fresh_count: 0,
        leads: []
      };
  const progressForToday = progressState.days?.[today] || {};
  const preparedLeads = (cachedToday.leads || []).map(lead => ({
    ...lead,
    manual_research: progressForToday[lead.run_id] || {
      status: 'not_started',
      notes: '',
      updated_at: null
    }
  }));
  const matchingRun = latestMatchingLeadRun(preparedLeads);
  if (matchingRun.file) {
    runPath = matchingRun.file;
    run = matchingRun.data;
    overlapScan = run.overlap_scan || run.archive_reconciliation || {};
  }
  const manualResearchCompleted = preparedLeads.filter(
    lead => lead.manual_research?.status === 'completed'
  ).length;
  const approvedReviewLeads = preparedLeads.filter(lead => lead.approved);
  const matchingComputation = latestMatchingLeadComputation(preparedLeads);
  const computationByLeadId = new Map(
    (matchingComputation.data.leads || []).map(lead => [String(lead.lead_id || ''), lead])
  );
  const activity = readActivityDashboard(today);
  const activityLeadIds = new Set(activity.lead_ids);
  const approvalGateEnabled = Boolean(config.approval_gate_enabled);
  let processingSource = [];
  let selectionMode = 'waiting_for_review_handoff';
  if (computationByLeadId.size) {
    processingSource = preparedLeads.filter(lead => computationByLeadId.has(String(lead.run_id || '')));
    selectionMode = 'computation_state';
  } else if (!approvalGateEnabled) {
    processingSource = preparedLeads;
    selectionMode = 'approval_disabled';
  } else if (cachedToday.review_complete) {
    processingSource = approvedReviewLeads;
    selectionMode = 'approved_only';
  }
  const processingLeads = processingSource
    .filter(lead => !activityLeadIds.has(String(lead.run_id || '')))
    .map(lead => {
    const computationLead = computationByLeadId.get(String(lead.run_id || '')) || {};
    return {
      ...lead,
      processing_status: computationLead.status || 'awaiting_processing',
      executive_count: (computationLead.executives || []).length,
      executives: computationLead.executives || [],
      search_tasks_total: (computationLead.search_tasks || []).length,
      destination_row: computationDestinationRow(computationLead),
      processing_notes: computationLead.notes || []
    };
  });
  const computationMode = String(matchingComputation.data.mode || '');
  const manualReadyCount = (matchingComputation.data.leads || []).filter(manualLeadReady).length;
  const bridged = (matchingComputation.data.writes || [])
    .some(write => Number(write.rows_written || 0) > 0);
  const finalReport = processingFinalReport(
    matchingComputation.data,
    matchingComputation.file
  );
  const firstAutomationStatus = readAutomationStatus(PROCESS_APPROVED_FIRST_AUTOMATION_PATH);
  const fallbackAutomationStatus = readAutomationStatus(PROCESS_APPROVED_FALLBACK_AUTOMATION_PATH);
  const now = new Date();
  const firstDeadlineAt = new Date(`${today}T${config.first_review_deadline || '18:00'}:00`);
  const fallbackDeadlineAt = new Date(`${today}T${config.fallback_review_deadline || '22:00'}:00`);
  const nextDeadlineAt = now < firstDeadlineAt
    ? firstDeadlineAt
    : now < fallbackDeadlineAt ? fallbackDeadlineAt : fallbackDeadlineAt;
  const reviewWindow = now < firstDeadlineAt
    ? 'first'
    : now < fallbackDeadlineAt ? 'fallback' : 'closed';
  const reviewStatus = cachedToday.review_complete
    ? 'review_complete'
    : !preparedLeads.length
      ? 'waiting_for_group'
      : reviewWindow === 'first'
        ? 'review_due_before_first_run'
        : reviewWindow === 'fallback'
          ? 'first_run_missed'
          : 'fallback_due_or_passed';
  const watcher = readJsonFile(path.join(STATE_DIR, 'orchestration_watcher.json'), {});
  const leadCheckpoints = watcher.workflows?.lead_prep?.days?.[today]?.checkpoints || {};
  const todayCheckpoint = leadCheckpoints.lead_prep_prepare || {};
  const isRunning = activeProcessMeta?.workflow === 'lead_prep'
    || todayCheckpoint.status === 'running';
  const latestRunIsToday = runMatchesDay(run, today);
  const freshTarget = Number(overlapScan.fresh_target || config.base_volume);
  const freshCount = Number(Object.prototype.hasOwnProperty.call(overlapScan, 'fresh_count')
    ? overlapScan.fresh_count
    : cachedToday.fresh_count || 0);
  const archiveMatchCount = Number(Object.prototype.hasOwnProperty.call(overlapScan, 'archive_match_count')
    ? overlapScan.archive_match_count
    : cachedToday.archive_match_count || 0);
  const conflictCount = Number(Object.prototype.hasOwnProperty.call(overlapScan, 'conflict_count')
    ? overlapScan.conflict_count
    : cachedToday.conflict_count || 0);
  const targetMet = Object.prototype.hasOwnProperty.call(overlapScan, 'target_met')
    ? Boolean(overlapScan.target_met)
    : freshCount >= freshTarget;
  return {
    config,
    archive: {
      enabled: available > 0,
      total: entries.length,
      available,
      consumed,
      updated_at: archive.updated_at || null,
      path: LEAD_PREP_ARCHIVE_PATH
    },
    run: {
      exists: Boolean(run.run_id),
      is_today: latestRunIsToday,
      run_id: run.run_id || '',
      created_at: run.created_at || null,
      status: run.status || 'not_prepared',
      file: runPath || '',
      selected_count: Number(run.source?.selected_count || run.leads?.length || 0),
      fresh_target: freshTarget,
      fresh_count: freshCount,
      archive_match_count: archiveMatchCount,
      conflict_count: conflictCount,
      top_up_count: Number(overlapScan.top_up_count || 0),
      top_up_suppressed_count: Number(overlapScan.top_up_suppressed_count || 0),
      top_ups_enabled: Object.prototype.hasOwnProperty.call(overlapScan, 'top_ups_enabled')
        ? Boolean(overlapScan.top_ups_enabled)
        : true,
      target_met: targetMet,
      settled_for_day: Object.prototype.hasOwnProperty.call(overlapScan, 'settled_for_day')
        ? Boolean(overlapScan.settled_for_day)
        : targetMet,
      source_exhausted: Boolean(overlapScan.source_exhausted),
      enabled: Boolean(overlapScan.enabled),
      detection_only: run.overlap_scan ? Boolean(overlapScan.detection_only) : true,
      waves: overlapScan.waves || [],
      errors: run.errors || []
    },
    review: {
      ...cachedToday,
      leads: preparedLeads,
      manual_research_completed: manualResearchCompleted,
      status: reviewStatus,
      window: reviewWindow,
      first_deadline_at: firstDeadlineAt.toISOString(),
      fallback_deadline_at: fallbackDeadlineAt.toISOString(),
      next_deadline_at: nextDeadlineAt.toISOString(),
      cache: {
        path: LEAD_REVIEW_CACHE_PATH,
        cached_at: reviewCache.cached_at || null,
        cache_date: reviewCache.cache_date || null,
        checkpoint: leadCheckpoints.lead_review_daily_cache || {}
      },
      processing_automation: {
        first_status: firstAutomationStatus,
        fallback_status: fallbackAutomationStatus,
        paused: firstAutomationStatus !== 'ACTIVE' && fallbackAutomationStatus !== 'ACTIVE'
      }
    },
    historical: {
      ...(reviewCache.summary || {
        total_leads: 0,
        total_case_study_worthy: 0,
        total_approved: 0,
        date_groups: 0,
        reviewed_date_groups: 0
      }),
      recent_days: (reviewCache.history || []).slice(0, 14)
    },
    processing: {
      selection_mode: selectionMode,
      eligible_count: processingLeads.length,
      computation_file: matchingComputation.file,
      computation_status: matchingComputation.data.status || '',
      computation_mode: computationMode,
      manual_editable: !bridged,
      manual_ready_count: manualReadyCount,
      bridge_ready: Boolean(processingLeads.length)
        && manualReadyCount === processingLeads.length
        && computationMode === 'manual'
        && !bridged,
      bridged,
      report: finalReport,
      leads: processingLeads
    },
    activity,
    watcher: {
      status: todayCheckpoint.status || (config.autonomous_prep_enabled ? 'waiting' : 'disabled'),
      due: config.prep_time,
      completed_at: todayCheckpoint.completed_at || null,
      error: todayCheckpoint.result?.commands?.find(command => !command.ok)?.stderr_tail || ''
    },
    is_running: isRunning,
    active_action: activeProcessMeta?.workflow === 'lead_prep' ? activeProcessMeta.action : '',
    last_updated: run.created_at || archive.updated_at || watcher.last_tick_at || null
  };
}

function titleCaseStatus(value) {
  return String(value || '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, char => char.toUpperCase());
}

function obfRowStatus(events) {
  const hasType = type => events.some(event => event.event_type === type);
  const hasStatus = value => events.some(event => String(event.status || '').toLowerCase() === value);
  const hasPendingSync = events.some(event => String(event.status || '').toLowerCase() === 'pending_sync');
  const hasError = events.some(event => event.error || /fail|blocked|error/.test(String(event.status || '').toLowerCase()));
  const reconciliation = [...events].reverse().find(event => event.event_type === 'prospect_reconciliation');

  if (hasPendingSync) return { label: 'Sync Pending', tone: 'warning' };
  if (hasType('connection_request_confirmed') || hasStatus('sent')) return { label: 'Conn Sent', tone: 'success' };
  if (hasType('prospect_requires_email')) return { label: 'Requires Email', tone: 'warning' };
  if (reconciliation) return { label: titleCaseStatus(reconciliation.live_state || 'Reconciled'), tone: 'success' };
  if (hasError) return { label: 'Failed', tone: 'danger' };
  if (hasStatus('skipped_timeout')) return { label: 'Timed Out', tone: 'warning' };
  if (events.length) return { label: 'In Progress', tone: 'active' };
  return { label: 'Prepared', tone: 'neutral' };
}

function summarizeObfDay(date, prepared, events) {
  const grouped = new Map();
  events.forEach(event => {
    const key = String(event.prospect_id || '');
    if (!key) return;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(event);
  });

  const rows = (prepared.queue || []).map((prospect, index) => {
    const prospectEvents = grouped.get(String(prospect.id || '')) || [];
    const runtime = (prepared.runtime_plan || [])[index] || {};
    const status = obfRowStatus(prospectEvents);
    const latest = prospectEvents[prospectEvents.length - 1] || {};
    const retryCount = prospectEvents.filter(event =>
      /retry|recover|timeout/.test(`${event.event_type || ''} ${event.status || ''} ${event.stage || ''}`.toLowerCase())
    ).length;
    return {
      index: index + 1,
      id: prospect.id || '',
      company: prospect.company || 'Unknown company',
      contact_name: prospect.contact_name || 'Unknown contact',
      contact_title: prospect.contact_title || '',
      contact_linkedin: prospect.contact_linkedin || '',
      prospect_row: prospect._row_number || '',
      slot_id: runtime.slot_id || index + 1,
      activity_timing: runtime.activity_log_timing || '',
      delay_sec: runtime.delay_sec ?? null,
      diversion: runtime.lead_diversion || 'none',
      progress: status.label,
      tone: status.tone,
      retry_count: retryCount,
      latest_stage: latest.stage || latest.event_type || '',
      last_update: latest.recorded_at || '',
      detail: latest.error || latest.reason || latest.live_state || ''
    };
  });

  const sent = rows.filter(row => row.progress === 'Conn Sent').length;
  const reconciled = rows.filter(row => /Already Pending|Reconciled/.test(row.progress)).length;
  const requiresEmail = rows.filter(row => row.progress === 'Requires Email').length;
  const failed = rows.filter(row => row.progress === 'Failed').length;
  const pendingSync = rows.filter(row => row.progress === 'Sync Pending').length;
  const timedOut = rows.filter(row => row.progress === 'Timed Out').length;
  const resolved = sent + reconciled + requiresEmail + failed + pendingSync + timedOut;
  return {
    date,
    rows,
    planned: Number(prepared.planned_count || prepared.target_remaining || rows.length || 0),
    target: Number(prepared.target_total || 0),
    sent,
    reconciled,
    requires_email: requiresEmail,
    failed,
    pending_sync: pendingSync,
    timed_out: timedOut,
    resolved,
    retries: rows.reduce((total, row) => total + row.retry_count, 0),
    start_time: events[0]?.recorded_at || null,
    last_event_time: events[events.length - 1]?.recorded_at || null
  };
}

function obfIssues(events, watcherCheckpoint) {
  const issues = [];
  events.forEach(event => {
    const status = String(event.status || '').toLowerCase();
    const noteworthy = event.error
      || status === 'pending_sync'
      || status === 'skipped_timeout'
      || /fail|blocked|retry/.test(status);
    if (!noteworthy) return;
    issues.push({
      time: event.recorded_at || '',
      severity: event.error || /fail|blocked/.test(status) ? 'danger' : 'warning',
      title: event.company || titleCaseStatus(event.event_type || 'Workflow issue'),
      detail: event.error || event.reason || titleCaseStatus(event.stage || event.status),
      prospect_id: event.prospect_id || ''
    });
  });
  if (watcherCheckpoint && /failed|interrupted|blocked/.test(String(watcherCheckpoint.status || ''))) {
    issues.unshift({
      time: watcherCheckpoint.completed_at || watcherCheckpoint.interrupted_at || '',
      severity: 'danger',
      title: 'Autonomous checkpoint failed',
      detail: watcherCheckpoint.reason || watcherCheckpoint.result?.commands?.find(command => !command.ok)?.stderr_tail || 'Inspect the console output.',
      prospect_id: ''
    });
  }
  return issues.slice(-20).reverse();
}

function readObfDashboard() {
  const today = getTodayString();
  const preparedPath = path.join(STATE_DIR, 'outreach_sequences', `${today}-prepared.json`);
  const journalPath = path.join(STATE_DIR, 'outreach_journal', `${today}.jsonl`);
  const prepared = readJsonFile(preparedPath, {});
  const events = readJsonLines(journalPath);
  const summary = summarizeObfDay(today, prepared, events);
  const config = readObfConfig();
  const watcher = readJsonFile(path.join(STATE_DIR, 'orchestration_watcher.json'), {});
  const checkpoints = watcher.workflows?.obf?.days?.[today]?.checkpoints || {};
  const runningCheckpoint = Object.values(checkpoints).find(checkpoint => checkpoint.status === 'running');
  const latestCheckpoint = Object.values(checkpoints).sort((a, b) =>
    String(b.completed_at || b.started_at || '').localeCompare(String(a.completed_at || a.started_at || ''))
  )[0] || null;
  const isRunning = activeProcessMeta?.workflow === 'obf' || Boolean(runningCheckpoint);
  const activeAction = activeProcessMeta?.workflow === 'obf'
    ? activeProcessMeta.action
    : (runningCheckpoint ? (runningCheckpoint.name?.includes('prepare') ? 'prepare' : 'execute') : '');

  let phase = 'waiting';
  if (prepared.ready) phase = 'prepared';
  if (events.length) phase = 'executing';
  if (summary.planned > 0 && summary.resolved >= summary.planned) phase = 'completed';
  if (latestCheckpoint && /failed|interrupted/.test(String(latestCheckpoint.status || ''))) phase = 'failed';
  if (isRunning) phase = activeAction === 'prepare' ? 'preparing' : 'executing';

  const historyDir = path.join(STATE_DIR, 'outreach_sequences');
  const history = fs.existsSync(historyDir)
    ? fs.readdirSync(historyDir)
      .filter(file => /^\d{4}-\d{2}-\d{2}-prepared\.json$/.test(file))
      .sort((a, b) => b.localeCompare(a))
      .slice(0, 14)
      .map(file => {
        const date = file.slice(0, 10);
        const dayPrepared = readJsonFile(path.join(historyDir, file), {});
        const dayEvents = readJsonLines(path.join(STATE_DIR, 'outreach_journal', `${date}.jsonl`));
        const daySummary = summarizeObfDay(date, dayPrepared, dayEvents);
        return {
          date,
          prepared: daySummary.planned,
          sent: daySummary.sent,
          reconciled: daySummary.reconciled,
          issues: daySummary.failed + daySummary.pending_sync + daySummary.timed_out,
          completion: daySummary.planned ? Math.round((daySummary.resolved / daySummary.planned) * 100) : 0,
          started_at: daySummary.start_time,
          completed_at: daySummary.last_event_time
        };
      })
    : [];

  return {
    today,
    preview: false,
    config,
    phase,
    is_running: isRunning,
    active_action: activeAction,
    prepared_exists: Boolean(prepared.ready),
    prepared_at: prepared.prepared_at || null,
    approval: prepared.approval_state || null,
    control: prepared.outreach_control || null,
    state_path: preparedPath,
    journal_path: journalPath,
    summary,
    issues: obfIssues(events, latestCheckpoint),
    checkpoints,
    history,
    last_updated: summary.last_event_time || prepared.prepared_at || watcher.last_tick_at || null
  };
}

function writeObfConfig(payload) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(OBF_CONFIG_PATH, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function writeLeadPrepConfig(payload) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(LEAD_PREP_CONFIG_PATH, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function writeJsonFile(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function runProjectPython(scriptPath, args = []) {
  return new Promise(resolve => {
    const child = spawn('python3', [path.join(PROJECT_DIR, scriptPath), ...args], {
      cwd: PROJECT_DIR,
      env: { ...process.env, PYTHONUNBUFFERED: '1' }
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', data => { stdout += data.toString(); });
    child.stderr.on('data', data => { stderr += data.toString(); });
    child.on('close', code => resolve({ ok: code === 0, code, stdout, stderr }));
  });
}

function runCapturedPython(args) {
  return new Promise((resolve, reject) => {
    const child = spawn('python3', [path.join(PROJECT_DIR, 'helpers', 'linkedin_outreach_session.py'), ...args], {
      cwd: PROJECT_DIR,
      env: { ...process.env, PYTHONUNBUFFERED: '1' }
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', data => { stdout += data.toString(); });
    child.stderr.on('data', data => { stderr += data.toString(); });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || stdout.trim() || `Command exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        resolve({ ok: true, output: stdout.trim() });
      }
    });
  });
}

function createFloatingWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();

  floatingWindow = new BrowserWindow({
    width: 68,
    height: 68,
    x: 12,
    y: 35,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    visibleOnAllWorkspaces: true,
    resizable: false,
    skipTaskbar: true,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  });

  floatingWindow.loadFile(path.join(__dirname, 'floating.html'));
  // macOS needs both calls for a small utility window to remain available on
  // full-screen Spaces, not merely above normal application windows.
  floatingWindow.setAlwaysOnTop(true, 'screen-saver');
  floatingWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  preventExternalNavigation(floatingWindow);

  floatingWindow.on('closed', () => {
    floatingWindow = null;
  });
}

function createDrawerWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const workArea = primaryDisplay.workArea;
  const dashboardWidth = Math.min(1180, workArea.width - 40);
  const dashboardHeight = Math.min(900, workArea.height - 32);

  drawerWindow = new BrowserWindow({
    width: dashboardWidth,
    height: dashboardHeight,
    minWidth: Math.min(960, workArea.width - 20),
    minHeight: Math.min(680, workArea.height - 20),
    x: Math.round(workArea.x + (workArea.width - dashboardWidth) / 2),
    y: Math.round(workArea.y + (workArea.height - dashboardHeight) / 2),
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    vibrancy: 'under-window',
    visualEffectState: 'active',
    show: false,
    alwaysOnTop: false,
    resizable: true,
    fullscreenable: true,
    simpleFullscreen: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  });

  drawerWindow.loadFile(path.join(__dirname, 'dashboard.html'));
  preventExternalNavigation(drawerWindow);
  drawerWindow.once('ready-to-show', () => {
    if (!drawerWindow) return;
    drawerWindow.show();
    drawerWindow.focus();
    drawerWindow.webContents.send('slide-in');
  });

  drawerWindow.on('closed', () => {
    drawerWindow = null;
  });
}

function preventExternalNavigation(window) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => {
    event.preventDefault();
  });
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: app.name,
      submenu: [{ role: 'hide' }, { role: 'hideOthers' }, { type: 'separator' }, { role: 'quit' }]
    },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' }
  ]));
  createFloatingWindow();
  createDrawerWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createFloatingWindow();
      createDrawerWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// IPC handlers
ipcMain.on('toggle-drawer', () => {
  if (!drawerWindow) return;

  if (drawerWindow.isVisible()) {
    drawerWindow.webContents.send('slide-out');
    setTimeout(() => {
      if (drawerWindow) drawerWindow.hide();
    }, 300);
  } else {
    // Refresh stats before showing
    drawerWindow.webContents.send('refresh-data');
    drawerWindow.show();
    drawerWindow.focus();
    drawerWindow.webContents.send('slide-in');
  }
});

ipcMain.on('toggle-fullscreen', (event) => {
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window) return;
  const next = !window.isFullScreen();
  window.setFullScreen(next);
  event.sender.send('fullscreen-changed', next);
});

function cdpPortOpen(timeoutMs = 700) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: CHROME_CDP_HOST, port: CHROME_CDP_PORT });
    let settled = false;
    const finish = (isOpen) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(isOpen);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

function waitForChromeCdp(attempts = 15, delayMs = 2000) {
  return new Promise((resolve) => {
    let attempt = 0;
    const check = async () => {
      if (await cdpPortOpen()) return resolve(true);
      attempt += 1;
      if (attempt >= attempts) return resolve(false);
      setTimeout(check, delayMs);
    };
    check();
  });
}

async function ensureAutomationChrome(event) {
  if (await cdpPortOpen()) {
    event.reply('command-output', `>>> Chrome CDP already available on port ${CHROME_CDP_PORT}.\n`);
    return true;
  }

  if (!fs.existsSync(CHROME_LAUNCH_SCRIPT)) {
    event.reply('command-output', `>>> Error: Chrome launch script not found at ${CHROME_LAUNCH_SCRIPT}\n`);
    return false;
  }

  event.reply('command-output', '>>> Chrome CDP is offline. Launching the automation Chrome profile...\n');
  try {
    const chromeProc = spawn('bash', [CHROME_LAUNCH_SCRIPT], {
      detached: true,
      stdio: 'ignore'
    });
    chromeProc.unref();
  } catch (error) {
    event.reply('command-output', `>>> Error launching automation Chrome: ${error.message}\n`);
    return false;
  }

  const ready = await waitForChromeCdp();
  event.reply(
    'command-output',
    ready
      ? `>>> Chrome CDP is ready on port ${CHROME_CDP_PORT}. Starting Activity Check.\n`
      : `>>> Error: Chrome CDP did not become available on port ${CHROME_CDP_PORT}. Activity Check was not started.\n`
  );
  return ready;
}

ipcMain.on('run-command', async (event, { scriptPath, args }) => {
  if (activeProcess || activeProcessStarting) {
    event.reply('command-error', 'Another task is already running.');
    return;
  }

  const fullScriptPath = path.join(PROJECT_DIR, scriptPath);
  const isObf = scriptPath.endsWith('helpers/linkedin_outreach_session.py')
    || scriptPath.endsWith('scripts/run_outreach_lanes.py');
  const isLeadPrep = scriptPath.endsWith('scripts/lead_exec_research.py');
  const isActivityCheck = scriptPath.endsWith('scripts/check_prefinal_activity.py');
  const activityMode = args.includes('--prepare-only')
    ? 'prepare-only'
    : args.includes('--activity-only')
      ? 'activity-only'
      : 'full';
  activeProcessStarting = true;
  activeProcessMeta = isObf
    ? {
        workflow: 'obf',
        action: args[0] === 'prepare-8_30-session' ? 'prepare' : 'execute',
        started_at: new Date().toISOString()
      }
    : isLeadPrep
      ? {
          workflow: 'lead_prep',
          action: args[0] || 'run',
          started_at: new Date().toISOString()
        }
      : null;

  // Activity Check is always owned by the watcher. This keeps the dashboard
  // button, its CDP bootstrap, preflight, locking, and the 9 PM schedule on
  // one execution path rather than letting the UI run the worker directly.
  const command = isActivityCheck
    ? {
        executable: 'python3',
        args: [
          WATCHER_SCRIPT,
          '--force-activity-date',
          String(readLeadPrepConfig().dashboard_date_override || getTodayString()),
          '--force-activity-mode', activityMode
        ]
      }
    : { executable: 'python3', args: [fullScriptPath, ...args] };

  // Start the selected worker.
  try {
    if (isActivityCheck) {
      event.reply('command-output', `>>> Launching Activity Check through watcher (${activityMode}).\n`);
    }
    activeProcess = spawn(command.executable, command.args, {
      cwd: PROJECT_DIR,
      env: { ...process.env, PYTHONUNBUFFERED: '1' }
    });
  } catch (error) {
    activeProcessStarting = false;
    activeProcessMeta = null;
    event.reply('command-output', `Failed to start process: ${error.message}\n`);
    event.reply('command-exit', -1);
    return;
  }
  activeProcessStarting = false;

  activeProcess.stdout.on('data', (data) => {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', data.toString());
    }
  });

  activeProcess.stderr.on('data', (data) => {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', data.toString());
    }
  });

  activeProcess.on('close', (code) => {
    activeProcess = null;
    activeProcessMeta = null;
    if (drawerWindow) {
      drawerWindow.webContents.send('command-exit', code);
    }
  });

  activeProcess.on('error', (err) => {
    activeProcess = null;
    activeProcessStarting = false;
    activeProcessMeta = null;
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', `Failed to start process: ${err.message}\n`);
      drawerWindow.webContents.send('command-exit', -1);
    }
  });
});

ipcMain.on('stop-command', (event) => {
  if (activeProcess) {
    activeProcess.kill('SIGINT');
    setTimeout(() => {
      if (activeProcess) {
        activeProcess.kill('SIGKILL');
      }
    }, 1000);
    event.reply('command-stopped');
  }
});

ipcMain.on('run-job-discovery-command', (event, { action }) => {
  const allowed = new Set(['run', 'reverify']);
  if (!allowed.has(action)) {
    event.reply('command-error', 'Unsupported Daily Job Discovery action.');
    return;
  }
  if (!jobDiscoveryAvailable()) {
    event.reply('command-error', `Daily Job Discovery was not found at ${JOB_DISCOVERY_DIR}.`);
    return;
  }
  if (activeProcess || activeProcessStarting) {
    event.reply('command-error', 'Another task is already running.');
    return;
  }
  activeProcessStarting = true;
  activeProcessMeta = { workflow: 'job_discovery', action, started_at: new Date().toISOString() };
  try {
    activeProcess = spawn('node', ['src/cli.mjs', action], {
      cwd: JOB_DISCOVERY_DIR,
      env: { ...process.env }
    });
  } catch (error) {
    activeProcessStarting = false;
    activeProcessMeta = null;
    event.reply('command-output', `Failed to start Daily Job Discovery: ${error.message}\n`);
    event.reply('command-exit', -1);
    return;
  }
  activeProcessStarting = false;
  activeProcess.stdout.on('data', data => drawerWindow?.webContents.send('command-output', data.toString()));
  activeProcess.stderr.on('data', data => drawerWindow?.webContents.send('command-output', data.toString()));
  activeProcess.on('close', code => {
    activeProcess = null;
    activeProcessMeta = null;
    drawerWindow?.webContents.send('command-exit', code);
  });
  activeProcess.on('error', error => {
    activeProcess = null;
    activeProcessStarting = false;
    activeProcessMeta = null;
    drawerWindow?.webContents.send('command-output', `Failed to run Daily Job Discovery: ${error.message}\n`);
    drawerWindow?.webContents.send('command-exit', -1);
  });
});

// Check watcher status
ipcMain.handle('check-watcher-status', async () => {
  return new Promise((resolve) => {
    exec('/bin/launchctl list | grep com.outreachautomation.orchestration-watcher', (error, stdout) => {
      resolve(!!stdout && stdout.trim().length > 0);
    });
  });
});

// Toggle watcher
ipcMain.handle('toggle-watcher', async (event, enable) => {
  return new Promise((resolve) => {
    if (enable) {
      const plistSrc = path.join(PROJECT_DIR, 'orchestrator', 'watcher', 'com.outreachautomation.orchestration-watcher.plist.template');
      const plistTarget = path.join(os.homedir(), 'Library', 'LaunchAgents', 'com.outreachautomation.orchestration-watcher.plist');
      const cmd = `mkdir -p "${path.dirname(plistTarget)}" && sed "s|__PROJECT_ROOT__|${PROJECT_DIR}|g" "${plistSrc}" > "${plistTarget}" && launchctl unload "${plistTarget}" 2>/dev/null || true && launchctl load "${plistTarget}"`;
      exec(cmd, (error) => resolve(!error));
    } else {
      const cmd = 'launchctl unload ~/Library/LaunchAgents/com.outreachautomation.orchestration-watcher.plist 2>/dev/null || true';
      exec(cmd, (error) => resolve(!error));
    }
  });
});

ipcMain.handle('read-obf-dashboard', async () => readObfDashboard());
ipcMain.handle('read-lead-prep-dashboard', async () => readLeadPrepDashboard());
ipcMain.handle('read-post-engagement-dashboard', async (event, requested = {}) => {
  const args = ['status'];
  if (requested.day) args.push('--day', String(requested.day));
  const result = await runProjectPython('scripts/post_engagement.py', args);
  if (!result.ok) return { config: {}, campaign: {}, history: [], high_signal: [], error: result.stderr || result.stdout };
  try {
    const state = JSON.parse(result.stdout);
    return { ...state, is_running: state.is_running || Boolean(postEngagementProcess) };
  } catch (error) {
    return { config: {}, campaign: {}, history: [], high_signal: [], error: error.message };
  }
});
ipcMain.handle('add-post-engagement-source', async (event, requested = {}) => {
  const args = ['add-source', String(requested.url || '')];
  if (requested.day) args.push('--day', String(requested.day));
  if (requested.cdp_account) args.push('--cdp-account', String(requested.cdp_account));
  const result = await runProjectPython('scripts/post_engagement.py', args);
  return result.ok ? { ok: true } : { ok: false, error: result.stderr || result.stdout };
});
ipcMain.handle('save-post-engagement-settings', async (event, requested = {}) => {
  const result = await runProjectPython('scripts/post_engagement.py', ['configure', JSON.stringify(requested)]);
  return result.ok ? { ok: true } : { ok: false, error: result.stderr || result.stdout };
});
ipcMain.handle('pause-post-engagement-run', async (event, requested = {}) => {
  const args = ['pause'];
  if (requested.day) args.push('--day', String(requested.day));
  const result = await runProjectPython('scripts/post_engagement.py', args);
  return result.ok ? { ok: true } : { ok: false, error: result.stderr || result.stdout };
});
function startPostEngagement(execute) {
  if (postEngagementProcess) return { ok: false, error: 'Post Engagement is already running.' };
  const args = [path.join(PROJECT_DIR, 'scripts', 'post_engagement.py'), 'run'];
  if (execute) args.push('--execute');
  // Keep the Mac awake across the persisted 90-minute inter-batch waits.
  const hasCaffeinate = process.platform === 'darwin' && fs.existsSync('/usr/bin/caffeinate');
  const child = hasCaffeinate
    ? spawn('/usr/bin/caffeinate', ['-i', 'python3', ...args], { cwd: PROJECT_DIR, env: { ...process.env, PYTHONUNBUFFERED: '1' } })
    : spawn('python3', args, { cwd: PROJECT_DIR, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
  postEngagementProcess = child;
  child.stdout.on('data', data => drawerWindow?.webContents.send('command-output', data.toString()));
  child.stderr.on('data', data => drawerWindow?.webContents.send('command-output', data.toString()));
  child.on('close', code => {
    postEngagementProcess = null;
    drawerWindow?.webContents.send('command-output', `Post Engagement finished with code ${code}.\n`);
    drawerWindow?.webContents.send('post-engagement-updated');
  });
  child.on('error', error => {
    postEngagementProcess = null;
    drawerWindow?.webContents.send('command-output', `Post Engagement failed to start: ${error.message}\n`);
  });
  return { ok: true, pid: child.pid };
}
ipcMain.handle('start-post-engagement-dry-run', async () => startPostEngagement(false));
ipcMain.handle('start-post-engagement-run', async () => startPostEngagement(true));
ipcMain.handle('resume-post-engagement-run', async (event, requested = {}) => {
  const args = ['resume'];
  if (requested.day) args.push('--day', String(requested.day));
  const result = await runProjectPython('scripts/post_engagement.py', args);
  if (!result.ok) return { ok: false, error: result.stderr || result.stdout };
  return startPostEngagement(true);
});
ipcMain.handle('read-job-discovery-dashboard', async () => readJobDiscoveryDashboard());
ipcMain.handle('save-job-discovery-settings', async (event, requested) => saveJobDiscoverySettings(requested));
ipcMain.handle('toggle-job-discovery-scheduler', async (event, requested) => {
  if (!jobDiscoveryAvailable()) return { ok: false, error: `Daily Job Discovery was not found at ${JOB_DISCOVERY_DIR}.` };
  const enabled = Boolean(typeof requested === 'object' ? requested.enabled : requested);
  const script = enabled ? 'scripts/install-launchd.mjs' : 'scripts/uninstall-launchd.mjs';
  const result = await runJobDiscoveryNode([script], { timeoutMs: 30_000 });
  return result.ok
    ? { ok: true, enabled: jobDiscoverySchedulerEnabled() }
    : { ok: false, error: (result.stderr || result.stdout || 'Unable to change scheduler service.').trim() };
});
ipcMain.handle('refresh-lead-review-cache', async () => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before refreshing Lead Review.' };
  }
  const targetDate = String(readLeadPrepConfig().dashboard_date_override || '').trim();
  const result = await runProjectPython(
    'scripts/cache_lead_review_dashboard.py',
    targetDate ? ['--target-date', targetDate] : []
  );
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Lead Review cache refresh failed.' };
});

ipcMain.handle('save-lead-research-progress', async (event, requested) => {
  const day = String(requested.day || getTodayString());
  const runId = String(requested.run_id || '').trim();
  const allowedStatuses = new Set(['not_started', 'in_progress', 'completed', 'skipped']);
  const status = allowedStatuses.has(requested.status) ? requested.status : 'not_started';
  if (!runId) return { ok: false, error: 'A Lead Review run ID is required.' };
  const state = readJsonFile(LEAD_RESEARCH_PROGRESS_PATH, { schema_version: 1, days: {} });
  state.schema_version = 1;
  state.days = state.days || {};
  state.days[day] = state.days[day] || {};
  state.days[day][runId] = {
    status,
    notes: String(requested.notes || '').slice(0, 1000),
    updated_at: new Date().toISOString()
  };
  writeJsonFile(LEAD_RESEARCH_PROGRESS_PATH, state);
  return { ok: true, progress: state.days[day][runId] };
});

ipcMain.handle('update-lead-review-approval', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before changing an approval.' };
  }
  const row = Number(requested.row);
  const runId = String(requested.run_id || '').trim();
  if (!Number.isInteger(row) || row < 2 || !runId) {
    return { ok: false, error: 'The cached Lead Review row is invalid. Refresh the table and try again.' };
  }
  const result = await runProjectPython('scripts/update_lead_review_approval.py', [
    '--row',
    String(row),
    '--run-id',
    runId,
    '--approved',
    requested.approved ? 'true' : 'false'
  ]);
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Approval sync failed.' };
});

ipcMain.handle('update-lead-review-use', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before changing Use.' };
  }
  const row = Number(requested.row);
  const runId = String(requested.run_id || '').trim();
  const use = String(requested.use || '').trim();
  if (!Number.isInteger(row) || row < 2 || !runId) {
    return { ok: false, error: 'The cached Lead Review row is invalid. Refresh the table and try again.' };
  }
  const result = await runProjectPython('scripts/update_lead_review_use.py', [
    '--row',
    String(row),
    '--run-id',
    runId,
    '--use',
    use
  ]);
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Use sync failed.' };
});

ipcMain.handle('update-lead-review-complete', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before changing review completion.' };
  }
  const groupRow = Number(requested.group_row);
  const date = String(requested.date || '').trim();
  if (!Number.isInteger(groupRow) || groupRow < 2 || !date) {
    return { ok: false, error: 'The cached date group is invalid. Refresh the table and try again.' };
  }
  const result = await runProjectPython('scripts/update_lead_review_group_status.py', [
    '--group-row',
    String(groupRow),
    '--date',
    date,
    '--complete',
    requested.complete ? 'true' : 'false'
  ]);
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Review completion sync failed.' };
});

ipcMain.handle('sync-lead-review-group', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before completing review.' };
  }
  const groupRow = Number(requested.group_row);
  const date = String(requested.date || '').trim();
  if (!Number.isInteger(groupRow) || groupRow < 2 || !date || !Array.isArray(requested.rows) || !requested.rows.length) {
    return { ok: false, error: 'The cached review group is invalid. Refresh the table and try again.' };
  }
  const result = await runProjectPython('scripts/sync_lead_review_group.py', [
    '--payload-json',
    JSON.stringify(requested)
  ]);
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Review handoff failed.' };
});

ipcMain.handle('save-manual-lead-research', async (event, requested) => {
  const dashboard = readLeadPrepDashboard();
  if (!dashboard.processing?.manual_editable) {
    return { ok: false, error: 'This group has already been bridged and is read-only.' };
  }
  const leadId = String(requested.lead_id || '').trim();
  const allowedIds = new Set((dashboard.processing?.leads || []).map(lead => String(lead.run_id || '')));
  if (!allowedIds.has(leadId)) {
    return { ok: false, error: 'The selected lead is not in the active processing group.' };
  }
  try {
    const executives = normalizeManualExecutives(requested.executives);
    const { file, computation } = createManualComputation(dashboard);
    const lead = (computation.leads || []).find(item => String(item.lead_id || '') === leadId);
    if (!lead) throw new Error('The selected lead could not be found in the manual computation.');
    lead.executives = executives;
    lead.status = manualLeadReady(lead) ? 'manual_ready' : 'manual_draft';
    lead.updated_at = new Date().toISOString();
    const readyCount = (computation.leads || []).filter(manualLeadReady).length;
    computation.updated_at = new Date().toISOString();
    computation.status = readyCount === computation.leads.length
      ? 'manual_research_ready'
      : 'manual_research_in_progress';
    writeJsonFile(file, computation);
    return { ok: true, dashboard: readLeadPrepDashboard() };
  } catch (error) {
    return { ok: false, error: error.message || String(error) };
  }
});

ipcMain.handle('bridge-manual-lead-processing', async () => {
  const dashboard = readLeadPrepDashboard();
  const processing = dashboard.processing || {};
  if (processing.computation_mode !== 'manual' || !processing.computation_file) {
    return { ok: false, error: 'No manual research computation is ready to bridge.' };
  }
  if (processing.bridged) {
    return { ok: false, error: 'This manual research group has already been bridged.' };
  }
  if (!processing.bridge_ready) {
    return {
      ok: false,
      error: 'Complete P1 name, title, and LinkedIn for every lead before bridging.'
    };
  }
  const result = await runProjectPython('scripts/lead_exec_research.py', [
    'write-computation',
    '--computation-file',
    processing.computation_file
  ]);
  return result.ok
    ? { ok: true, dashboard: readLeadPrepDashboard() }
    : { ok: false, error: result.stderr || result.stdout || 'Manual research bridge failed.' };
});

ipcMain.handle('save-lead-prep-settings', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before changing Lead Prep settings.' };
  }
  const current = readLeadPrepConfig();
  const next = {
    ...current,
    autonomous_prep_enabled: Boolean(requested.autonomous_prep_enabled),
    prep_time: String(requested.prep_time || current.prep_time),
    base_volume: Math.max(1, Math.min(500, Number(requested.base_volume || current.base_volume))),
    processing_batch_size: 30,
    approval_gate_enabled: Boolean(requested.approval_gate_enabled),
    overlap_scan_mode: requested.overlap_scan_mode === 'off' ? 'off' : 'auto',
    fresh_volume_top_up_mode: requested.fresh_volume_top_up_mode === 'off' ? 'off' : 'auto'
  };
  if (!/^\d{2}:\d{2}$/.test(next.prep_time)) {
    return { ok: false, error: 'Preparation time must use HH:MM.' };
  }
  writeLeadPrepConfig(next);
  return { ok: true, config: next };
});

ipcMain.handle('save-obf-settings', async (event, requested) => {
  if (activeProcess) {
    return { ok: false, error: 'Wait for the active automation to finish before changing OBF settings.' };
  }
  const current = readObfConfig();
  const next = {
    ...current,
    enabled: Boolean(requested.enabled),
    prep_time: String(requested.prep_time || current.prep_time),
    exec_time: String(requested.exec_time || current.exec_time),
    max_sends: Math.max(1, Math.min(30, Number(requested.max_sends || current.max_sends))),
    daily_volume: Math.max(1, Math.min(30, Number(requested.daily_volume || current.daily_volume || 30))),
    prospects_start_row: Number(requested.prospects_start_row || current.prospects_start_row || 0) || null
  };
  if (!/^\d{2}:\d{2}$/.test(next.prep_time) || !/^\d{2}:\d{2}$/.test(next.exec_time)) {
    return { ok: false, error: 'Prep and execution times must use HH:MM.' };
  }

  const dashboard = readObfDashboard();
  const desiredVolume = Number(requested.daily_volume || dashboard.control?.effective_target || 30);
  const desiredStartRow = Number(requested.prospects_start_row || dashboard.control?.prospects_start_row || 0);
  const sheetChanged = requested.update_sheet !== false && (
    desiredVolume !== Number(dashboard.control?.effective_target || 0)
    || (desiredStartRow > 0 && desiredStartRow !== Number(dashboard.control?.prospects_start_row || 0))
    || Boolean(requested.approved) !== Boolean(dashboard.approval?.approved)
  );

  try {
    let sheetUpdate = null;
    if (sheetChanged) {
      const args = [
        'configure-outreach-control',
        '--date',
        getTodayString(),
        '--daily-volume',
        String(desiredVolume)
      ];
      if (desiredStartRow > 0) args.push('--prospects-start-row', String(desiredStartRow));
      args.push('--approved', requested.approved ? 'true' : 'false');
      sheetUpdate = await runCapturedPython(args);
    }
    writeObfConfig(next);
    return {
      ok: true,
      config: next,
      sheet_update: sheetUpdate,
      requires_reprep: Boolean(sheetChanged && dashboard.prepared_exists)
    };
  } catch (error) {
    return { ok: false, error: error.message || String(error) };
  }
});

// Read historical completions
ipcMain.handle('read-history', async () => {
  const history = {};

  // ... existing code ...

  const parseSessionFile = (filePath, category) => {
    try {
      if (!fs.existsSync(filePath)) return null;
      const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
      let completed = 0;
      let total = 0;

      if (category === 'activity') {
        const preparedTargets = data.prepared_targets || [];
        const resolvedTargets = data.targets || {};
        total = preparedTargets.length;
        completed = preparedTargets.filter(t => resolvedTargets[t.key] && resolvedTargets[t.key].status !== 'blank_uncertain_or_failed').length;
      } else if (category === 'followups') {
        const rawTargets = data.targets || [];
        const targets = Array.isArray(rawTargets) ? rawTargets : Object.values(rawTargets);
        total = targets.length;
        completed = targets.filter(t => t.outcome && !['Unsure', 'Failed'].includes(t.outcome)).length;
      } else if (category === 'withdrawals') {
        const rawTargets = data.queue || data.targets || [];
        const targets = Array.isArray(rawTargets) ? rawTargets : Object.values(rawTargets);
        total = targets.length;
        completed = targets.filter(t => t.outcome && t.outcome !== 'Failed').length;
      }
      return { total, completed };
    } catch (e) {
      return null;
    }
  };

  const scanDir = (dirName, category) => {
    const dirPath = path.join(STATE_DIR, dirName);
    if (!fs.existsSync(dirPath)) return;
    const files = fs.readdirSync(dirPath);
    for (const file of files) {
      if (file.endsWith('.json')) {
        const dateStr = file.replace('.json', '');
        const stats = parseSessionFile(path.join(dirPath, file), category);
        if (stats) {
          if (!history[dateStr]) history[dateStr] = { activity: null, followups: null, withdrawals: null };
          history[dateStr][category] = stats;
        }
      }
    }
  };

  scanDir('activity_sessions', 'activity');
  scanDir('followup_sessions', 'followups');
  scanDir('withdrawal_sessions', 'withdrawals');

  const sortedDates = Object.keys(history).sort((a, b) => b.localeCompare(a));
  return sortedDates.map(date => ({ date, ...history[date] }));
});

// Read and parse automation state files
ipcMain.handle('read-stats', async (event, requested = {}) => {
  // The Lead Prep test-date override must also drive Activity Check state;
  // otherwise the page can show a Jul 29 queue while this monitor reads Jul 30.
  const configuredDate = String(readLeadPrepConfig().dashboard_date_override || '').trim();
  const requestedDate = String(requested.date || '').trim();
  const today = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate)
    ? requestedDate
    : /^\d{4}-\d{2}-\d{2}$/.test(configuredDate)
      ? configuredDate
      : getTodayString();
  const stats = {
    today,
    activity: { prepared: 0, completed: 0, failed: 0, pending: 0, list: [] },
    followups: { prepared: 0, completed: 0, failed: 0, pending: 0, list: [] },
    withdrawals: { prepared: 0, completed: 0, failed: 0, pending: 0, list: [] },
    // Checkmark completion states driven by runner logs
    activity_prep_done: false,
    activity_run_done: false,
    followup_prep_done: false,
    followup_daytime_done: false,
    followup_monitor_done: false,
    followup_evening_done: false,
    followups_enabled: Boolean(readFollowupConfig().enabled),
    withdrawal_prep_done: false,
    withdrawal_execute_done: false,
    watcher: { tracked_days: 0, completed_days: 0, completion_rate: 0 },
    // Real-time pacing countdown state
    activity_pacing: null,
    withdrawal_pacing: null
  };

  stats.activity.is_running = false;
  stats.followups.is_running = false;
  stats.withdrawals.is_running = false;

  const watcherPath = path.join(STATE_DIR, 'orchestration_watcher.json');
  if (fs.existsSync(watcherPath)) {
    try {
      const wData = JSON.parse(fs.readFileSync(watcherPath, 'utf8'));
      const workflows = wData.workflows || {};
      const terminalStatuses = new Set(['completed', 'failed_terminal', 'cutoff_skipped', 'needs_attention', 'paused_manual_stop']);
      // The overview is a live daily view. Historical, unacknowledged alerts still
      // belong in watcher history, but must not be concatenated into today's banner.
      stats.watcher.alerts = (wData.alerts || []).filter(alert => (
        !alert.acknowledged
        && (alert.day === today || String(alert.created_at || '').startsWith(today))
      ));
      const queueDir = path.join(STATE_DIR, 'prefinal_queue');
      if (fs.existsSync(queueDir)) {
        for (const file of fs.readdirSync(queueDir).filter(name => name.endsWith('.json'))) {
          try {
            const queue = JSON.parse(fs.readFileSync(path.join(queueDir, file), 'utf8'));
            const issueCount = Object.keys(queue.activity_issues || {}).length;
            const queueDay = String(queue.updated_at || queue.prepared_at || queue.created_at || '').slice(0, 10);
            if (queueDay === today && issueCount && !['prospects_bridged', 'final_bridged'].includes(queue.status)) {
              stats.watcher.alerts.push({ workflow: 'activity', reason: `${issueCount}_quarantined_profile_issues`, checkpoint: queue.fingerprint || '' });
            }
          } catch (e) {}
        }
      }
      const watcherDays = new Map();
      for (const [wfName, wfData] of Object.entries(workflows)) {
        const days = wfData.days || {};
        for (const [day, dayData] of Object.entries(days)) {
          const cps = dayData.checkpoints || {};
          const checkpointStates = Object.values(cps);
          if (checkpointStates.length) watcherDays.set(day, [...(watcherDays.get(day) || []), ...checkpointStates]);
          for (const [cpName, cp] of Object.entries(cps)) {
            if (day === today && ['retry_waiting', 'resume_pending'].includes(cp.status)) {
              stats.watcher.alerts.push({
                workflow: wfName,
                reason: cp.status,
                checkpoint: cpName,
                next_retry_at: cp.next_retry_at || null,
                severity: 'info'
              });
            }
            if (day === today && ['failed_terminal', 'needs_attention', 'interrupted_terminal'].includes(cp.status)) {
              const commands = Array.isArray(cp.result?.commands) ? cp.result.commands : [];
              const output = commands.map(command => `${command.stdout_tail || ''}\n${command.stderr_tail || ''}`).join('\n');
              const blocked = output.match(/Blocked:\s*([^\n]+)/i);
              const detail = (blocked?.[1]?.trim() || String(cp.reason || cp.status || 'checkpoint failed').replaceAll('_', ' ')).replace(/["',}\s]+$/, '');
              const existing = stats.watcher.alerts.find(alert => alert.workflow === wfName && alert.checkpoint === cpName);
              if (existing) {
                existing.detail = detail;
                existing.severity = 'danger';
              } else {
                stats.watcher.alerts.push({ workflow: wfName, checkpoint: cpName, reason: cp.reason || cp.status, detail, severity: 'danger', day });
              }
            }
            if (cp.status === 'running' || cp.status === 'waiting') {
              if (wfName === 'activity') stats.activity.is_running = true;
              if (wfName === 'followups') stats.followups.is_running = true;
              if (wfName === 'withdrawals') stats.withdrawals.is_running = true;
            }
          }
        }
      }
      // A tracked day is one where every scheduled watcher checkpoint has reached
      // a terminal state. It counts as complete only when none of them failed/skipped.
      for (const checkpointStates of watcherDays.values()) {
        if (!checkpointStates.every(cp => terminalStatuses.has(cp.status))) continue;
        stats.watcher.tracked_days++;
        if (checkpointStates.every(cp => cp.status === 'completed')) stats.watcher.completed_days++;
      }
      const finalizedCheckpoints = [...watcherDays.values()].flat().filter(cp => terminalStatuses.has(cp.status));
      stats.watcher.finalized_checkpoints = finalizedCheckpoints.length;
      stats.watcher.completed_checkpoints = finalizedCheckpoints.filter(cp => cp.status === 'completed').length;
      stats.watcher.completion_rate = stats.watcher.finalized_checkpoints
        ? Math.round((stats.watcher.completed_checkpoints / stats.watcher.finalized_checkpoints) * 100)
        : 0;
      stats.watcher.history = [...watcherDays.entries()]
        .sort(([a], [b]) => b.localeCompare(a))
        .map(([day, checkpointStates]) => {
          const completed = checkpointStates.filter(cp => cp.status === 'completed').length;
          const finalized = checkpointStates.every(cp => terminalStatuses.has(cp.status));
          return { date: day, completed, scheduled: checkpointStates.length, rate: Math.round((completed / checkpointStates.length) * 100), outcome: completed === checkpointStates.length ? 'Completed' : (finalized ? 'Finalized with exceptions' : 'In progress') };
        });
      const seenAlerts = new Set();
      stats.watcher.alerts = stats.watcher.alerts.filter(alert => {
        const key = `${alert.workflow || ''}:${alert.checkpoint || ''}:${alert.reason || ''}`;
        if (seenAlerts.has(key)) return false;
        seenAlerts.add(key);
        return true;
      }).slice(0, 5);
    } catch (e) {}
  }


  const readJournalEvents = (category) => {
    const journalPath = path.join(STATE_DIR, `${category}_journal`, `${today}.jsonl`);
    const events = [];
    if (fs.existsSync(journalPath)) {
      try {
        const lines = fs.readFileSync(journalPath, 'utf8').trim().split('\n');
        for (const line of lines) {
          if (line.trim()) {
            events.push(JSON.parse(line));
          }
        }
      } catch (e) {
        console.error(`Error reading journal for ${category}:`, e);
      }
    }
    return events;
  };

  try {
    const actEvents = readJournalEvents('activity');
    const fuEvents = readJournalEvents('followup');
    const wdEvents = readJournalEvents('withdrawal');

    stats.activity.start_time = actEvents.length > 0 ? actEvents[0].recorded_at : null;
    stats.activity.last_event_time = actEvents.length > 0 ? actEvents[actEvents.length - 1].recorded_at : null;

    stats.followups.start_time = fuEvents.length > 0 ? fuEvents[0].recorded_at : null;
    stats.followups.last_event_time = fuEvents.length > 0 ? fuEvents[fuEvents.length - 1].recorded_at : null;

    stats.withdrawals.start_time = wdEvents.length > 0 ? wdEvents[0].recorded_at : null;
    stats.withdrawals.last_event_time = wdEvents.length > 0 ? wdEvents[wdEvents.length - 1].recorded_at : null;

    // 1. Parse Activity Check
    const actSessPath = path.join(STATE_DIR, 'activity_sessions', `${today}.json`);
    stats.activity_prep_done = fs.existsSync(actSessPath);
    stats.activity_run_done = actEvents.some(e => e.event === 'final_bridge' || e.event === 'target_activity_read' || e.event === 'target_activity');

    if (fs.existsSync(actSessPath)) {
      const data = JSON.parse(fs.readFileSync(actSessPath, 'utf8'));
      const preparedTargets = data.prepared_targets || [];
      const resolvedTargets = data.targets || {};

      stats.activity.prepared = preparedTargets.length;
      stats.activity.list = preparedTargets.map(t => {
        const res = resolvedTargets[t.key] || {};

        // Try to extract a name from profile URL if no name is available
        let name = t.contact_name;
        if (!name && t.profile_url) {
          const parts = t.profile_url.split('/in/');
          if (parts.length > 1) {
            name = parts[1].split('/')[0].split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
          }
        }
        if (!name) name = t.company || 'Unknown';

        return {
          name: name,
          company: t.company || '',
          status: res.status || 'Pending',
          detail: `Row ${t.row_number}`
        };
      });

      stats.activity.pending = preparedTargets.filter(t => !resolvedTargets[t.key]).length;
      stats.activity.completed = preparedTargets.filter(t => resolvedTargets[t.key] && resolvedTargets[t.key].status !== 'blank_uncertain_or_failed').length;
      stats.activity.failed = preparedTargets.filter(t => resolvedTargets[t.key] && resolvedTargets[t.key].status === 'blank_uncertain_or_failed').length;

      // Calculate Activity Pacing Countdown
      if (actEvents.length > 0) {
        const lastEvent = actEvents[actEvents.length - 1];
        if (lastEvent && lastEvent.recorded_at && (lastEvent.event === 'target_activity' || lastEvent.event === 'target_activity_read')) {
          const lastEventTime = new Date(lastEvent.recorded_at).getTime();
          const elapsed = (Date.now() - lastEventTime) / 1000;

          const tIdx = preparedTargets.findIndex(t => t.key === lastEvent.key || t.lead_id === lastEvent.lead_id);
          if (tIdx !== -1) {
            const target = preparedTargets[tIdx];
            let delaySec = target.delay_sec || 45;
            let isGap = false;

            const nextTarget = preparedTargets[tIdx + 1];
            if (nextTarget && nextTarget.batch_number !== target.batch_number) {
              delaySec = target.batch_gap_sec || 246;
              isGap = true;
            }

            if (elapsed < delaySec) {
              stats.activity_pacing = {
                remaining_sec: Math.round(delaySec - elapsed),
                label: isGap ? 'Batch Gap' : 'Pacing Delay'
              };
            }
          }
        }
      }
    }

    // 2. Parse Follow-ups
    const fuSessPath = path.join(STATE_DIR, 'followup_sessions', `${today}.json`);
    stats.followup_prep_done = fs.existsSync(fuSessPath);
    stats.followup_daytime_done = fuEvents.some(e => e.event === 'followup_run_summary' && e.mode === 'run' && !e.dry_run);
    stats.followup_monitor_done = fuEvents.some(e => e.event === 'followup_run_summary' && e.mode === 'monitor');
    stats.followup_evening_done = fuEvents.some(e => e.event === 'followup_run_summary' && e.mode === 'evening');

    if (fs.existsSync(fuSessPath)) {
      const data = JSON.parse(fs.readFileSync(fuSessPath, 'utf8'));
      const rawTargets = data.targets || [];
      const targets = Array.isArray(rawTargets) ? rawTargets : Object.values(rawTargets);

      stats.followups.prepared = targets.length || Number(data.first_message_draft_count || 0);
      stats.followups.list = targets.map(t => ({
        name: t.contact_name || 'Unknown',
        company: t.company || '',
        status: t.outcome || 'Pending',
        detail: t.next_action || 'FU',
        message: t.message || ''
      }));
      stats.followups.pending = targets.length ? targets.filter(t => !t.outcome).length : stats.followups.prepared;
      stats.followups.completed = targets.filter(t => t.outcome && !['Unsure', 'Failed'].includes(t.outcome)).length;
      stats.followups.failed = targets.filter(t => ['Unsure', 'Failed'].includes(t.outcome)).length;
      stats.followups.due_today = stats.followups.pending;
      stats.followups.first_message_drafts = Number(data.first_message_draft_count || 0);
      stats.followups.second_message_due = targets.filter(t => ['FU-2', 'FU-3', 'FU-4'].includes(String(t.next_action || '').toUpperCase())).length;
      stats.followups.pipeline_assessed = targets.length + (data.skipped || []).length;
    }

    // 3. Parse Withdrawals
    const wdSessPath = path.join(STATE_DIR, 'withdrawal_sessions', `${today}.json`);
    const wdSessionExists = fs.existsSync(wdSessPath);
    stats.withdrawal_prep_done = wdSessionExists;
    stats.withdrawal_execute_done = wdSessionExists && wdEvents.some(e => e.event === 'withdrawal_run_summary' && e.ok && !e.dry_run);

    if (wdSessionExists) {
      const data = JSON.parse(fs.readFileSync(wdSessPath, 'utf8'));
      const rawTargets = (data.runtime_plan && data.runtime_plan.queue) || data.queue || data.targets || [];
      const targets = Array.isArray(rawTargets) ? rawTargets : Object.values(rawTargets);

      stats.withdrawals.prepared = targets.length;
      stats.withdrawals.list = targets.map(t => ({
        name: t.contact_name || t.person_engaged || 'Unknown',
        company: t.company_name || t.company || '',
        status: t.outcome || 'Pending',
        detail: `Days Left: ${t.days_left ?? ''}`
      }));
      stats.withdrawals.pending = targets.filter(t => !t.outcome).length;
      stats.withdrawals.completed = targets.filter(t => t.outcome && t.outcome !== 'Failed').length;
      stats.withdrawals.failed = targets.filter(t => t.outcome === 'Failed').length;

      // Calculate Withdrawal Pacing Countdown
      if (wdEvents.length > 0) {
        const lastEvent = wdEvents[wdEvents.length - 1];
        if (lastEvent && lastEvent.recorded_at && lastEvent.event === 'withdrawal_target_result') {
          const lastEventTime = new Date(lastEvent.recorded_at).getTime();
          const elapsed = (Date.now() - lastEventTime) / 1000;

          const sessData = JSON.parse(fs.readFileSync(wdSessPath, 'utf8'));
          const batches = (sessData.runtime_plan && sessData.runtime_plan.batches) || [];

          let delaySec = 34; // default pacing fallback
          let isLastInBatch = false;

          for (const batch of batches) {
            const batchTargets = batch.targets || [];
            const idx = batchTargets.findIndex(bt => bt.prospect_id === lastEvent.prospect_id);
            if (idx !== -1) {
              if (idx === batchTargets.length - 1) {
                delaySec = batch.inter_batch_delay_sec || 211;
                isLastInBatch = true;
              } else {
                delaySec = batchTargets[idx].delay_sec || 34;
              }
              break;
            }
          }

          if (elapsed < delaySec) {
            stats.withdrawal_pacing = {
              remaining_sec: Math.round(delaySec - elapsed),
              label: isLastInBatch ? 'Inter-Batch Pacing' : 'Target Pacing'
            };
          }
        }
      }
    }

    // Check withdrawal cooldown guard
    const guardPath = path.join(STATE_DIR, 'withdrawal_run_guard.json');
    if (fs.existsSync(guardPath)) {
      try {
        const guardData = JSON.parse(fs.readFileSync(guardPath, 'utf8'));
        stats.withdrawal_cooldown = guardData.last_withdrawal_time || null;
      } catch (e) {}
    }

  } catch (err) {
    console.error('Error reading stats:', err);
  }

  return stats;
});

// System Keep-Awake (Caffeinate) IPC listeners
let caffeinateProcess = null;

ipcMain.on('start-caffeinate', (event, { durationSeconds }) => {
  if (caffeinateProcess) {
    caffeinateProcess.kill();
    caffeinateProcess = null;
  }

  const args = [];
  if (durationSeconds !== 'infinite') {
    args.push('-t', durationSeconds);
  }
  // -d prevents display sleep, -i prevents idle sleep
  args.push('-d', '-i');

  caffeinateProcess = spawn('caffeinate', args);

  if (drawerWindow) {
    drawerWindow.webContents.send('command-output', `>>> System keep-awake active (Caffeinated: ${durationSeconds === 'infinite' ? 'Indefinite' : durationSeconds + 's'})\n`);
  }

  caffeinateProcess.on('exit', (code) => {
    caffeinateProcess = null;
    if (drawerWindow) {
      drawerWindow.webContents.send('caffeinate-stopped');
      drawerWindow.webContents.send('command-output', `>>> Caffeinate keep-awake terminated (code ${code})\n`);
    }
  });
});

ipcMain.on('stop-caffeinate', () => {
  if (caffeinateProcess) {
    caffeinateProcess.kill();
    caffeinateProcess = null;
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', `>>> Caffeinate manually stopped.\n`);
    }
  }
});

// Launch Automation Chrome Profile IPC listener
ipcMain.on('launch-chrome', (event, target = 'cdp1') => {
  const scripts = target === 'both' ? [CHROME_LAUNCH_SCRIPT, CHROME_2_LAUNCH_SCRIPT] : [target === 'cdp2' ? CHROME_2_LAUNCH_SCRIPT : CHROME_LAUNCH_SCRIPT];
  if (scripts.some(script => !fs.existsSync(script))) {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', `>>> Error: Chrome launch script not found at ${CHROME_LAUNCH_SCRIPT}\n`);
    }
    return;
  }

  for (const script of scripts) {
  const chromeProc = spawn('bash', [script]);

  chromeProc.stdout.on('data', (data) => {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', data.toString());
    }
  });

  chromeProc.stderr.on('data', (data) => {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', data.toString());
    }
  });

  chromeProc.on('exit', (code) => {
    if (drawerWindow) {
      drawerWindow.webContents.send('command-output', `>>> Chrome browser process closed (exited with code ${code})\n`);
    }
  });
  }
});
