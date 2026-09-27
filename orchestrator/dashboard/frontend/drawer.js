const isElectronRuntime = Boolean(window.process && window.process.versions && window.process.versions.electron);

function createBrowserPreviewIpc() {
  const listeners = new Map();
  let previewWatcherActive = false;
  let previewObfConfig = {
    enabled: false,
    prep_time: '08:25',
    exec_time: '08:30',
    max_sends: 20,
    daily_volume: 30,
    prospects_start_row: 230,
    timezone: 'Africa/Lagos'
  };
  let previewObfPhase = 'executing';
  let previewObfRunning = false;
  let previewObfVolume = 12;
  let previewObfStartRow = 230;
  let previewLeadPrepConfig = {
    autonomous_prep_enabled: false,
    prep_time: '14:00',
    first_review_deadline: '18:00',
    fallback_review_deadline: '22:00',
    base_volume: 60,
    processing_batch_size: 30,
    approval_gate_enabled: false,
    overlap_scan_mode: 'auto',
    fresh_volume_top_up_mode: 'auto'
  };
  let previewLeadPrepRunning = false;
  const previewCompanies = [
    ['Vinci Capital', 'Selale Zaim', 'Head of Investment Committee'],
    ['Ampersand Capital Partners', 'Herb H. Hooper', 'Managing Partner'],
    ['Kuijpers & Lievaart', 'Jan Paul Lievaart', 'Partner'],
    ['Avida International', 'Dorothee Franzen', 'Managing Director'],
    ['Cross Ocean Ventures', 'Serhat Pala', 'Partner'],
    ['Cyclus Property', 'Roger Heaver', 'Founder'],
    ['Financieringsgilde Amsterdam', 'Bob van Overloop', 'Managing Partner'],
    ['FNO', 'Ann Kusters', 'Director'],
    ['FRIS Property Brokers', 'Hans Peter Fris', 'Partner'],
    ['Golden Egg Check', 'Thomas Mensink', 'Founder'],
    ['Homerun Capital', 'Joris van der Gucht', 'Investment Manager'],
    ['Tel Brokers', 'Mark Koster', 'Managing Director']
  ];

  const previewObfRows = previewCompanies.map((entry, index) => {
    let progress = 'Prepared';
    let tone = 'neutral';
    let detail = '';
    if (index < 7) {
      progress = 'Conn Sent';
      tone = 'success';
    } else if (index === 7) {
      progress = 'Requires Email';
      tone = 'warning';
    } else if (index === 8) {
      progress = 'Already Pending';
      tone = 'success';
    } else if (index === 9) {
      progress = 'Timed Out';
      tone = 'warning';
      detail = 'Activity feed did not hydrate';
    } else if (index === 10) {
      progress = 'In Progress';
      tone = 'active';
    }
    return {
      index: index + 1,
      id: `20260729-${String(index + 1).padStart(3, '0')}`,
      company: entry[0],
      contact_name: entry[1],
      contact_title: entry[2],
      contact_linkedin: '#',
      prospect_row: 230 + index,
      slot_id: index + 1,
      activity_timing: index % 2 ? 'after_conn' : 'before_conn',
      delay_sec: 24 + (index * 4),
      diversion: index === 5 ? 'feed' : 'none',
      progress,
      tone,
      retry_count: index === 9 ? 1 : 0,
      latest_stage: progress === 'Prepared' ? '' : progress.toLowerCase().replaceAll(' ', '_'),
      last_update: progress === 'Prepared' ? '' : `2026-07-29T09:${String(3 + index * 2).padStart(2, '0')}:00`,
      detail
    };
  });

  const previewObfDashboard = () => {
    const rows = previewObfPhase === 'waiting' ? [] : previewObfRows;
    const sent = rows.filter(row => row.progress === 'Conn Sent').length;
    const reconciled = rows.filter(row => row.progress === 'Already Pending').length;
    const requiresEmail = rows.filter(row => row.progress === 'Requires Email').length;
    const timedOut = rows.filter(row => row.progress === 'Timed Out').length;
    const pendingSync = rows.filter(row => row.progress === 'Sync Pending').length;
    const resolved = sent + reconciled + requiresEmail + timedOut + pendingSync;
    return {
      today: new Date().toISOString().slice(0, 10),
      preview: true,
      config: previewObfConfig,
      phase: previewObfPhase,
      is_running: previewObfRunning,
      active_action: previewObfRunning ? (previewObfPhase === 'preparing' ? 'prepare' : 'execute') : '',
      prepared_exists: rows.length > 0,
      prepared_at: rows.length ? '2026-07-29T08:25:42' : null,
      approval: rows.length ? { approved: true, status: 'Planned' } : null,
      control: rows.length ? { effective_target: previewObfVolume, current_progress: sent, prospects_start_row: previewObfStartRow, status: 'Partial' } : null,
      state_path: 'state/outreach_sequences/2026-07-29-prepared.json',
      journal_path: 'state/outreach_journal/2026-07-29.jsonl',
      summary: {
        rows,
        planned: rows.length,
        target: rows.length,
        sent,
        reconciled,
        requires_email: requiresEmail,
        failed: 0,
        pending_sync: pendingSync,
        timed_out: timedOut,
        resolved,
        retries: timedOut,
        start_time: rows.length ? '2026-07-29T09:03:02' : null,
        last_event_time: rows.length ? '2026-07-29T09:24:00' : null
      },
      issues: rows.length ? [{
        time: '2026-07-29T09:22:00',
        severity: 'warning',
        title: 'Golden Egg Check',
        detail: 'Activity feed did not hydrate; target timed out and remains unsent.',
        prospect_id: '20260729-010'
      }] : [],
      checkpoints: {},
      history: [
        { date: '2026-07-09', prepared: 20, sent: 18, reconciled: 1, issues: 1, completion: 100, started_at: '2026-07-09T09:03:02', completed_at: '2026-07-09T09:49:14' },
        { date: '2026-07-08', prepared: 20, sent: 20, reconciled: 0, issues: 0, completion: 100, started_at: '2026-07-08T08:31:11', completed_at: '2026-07-08T09:19:03' },
        { date: '2026-07-07', prepared: 18, sent: 16, reconciled: 1, issues: 1, completion: 100, started_at: '2026-07-07T08:32:08', completed_at: '2026-07-07T09:15:31' }
      ],
      last_updated: rows.length ? '2026-07-29T09:24:00' : null
    };
  };

  const unavailableObfDashboard = () => ({
    today: new Date().toISOString().slice(0, 10),
    preview: false,
    unavailable: true,
    config: previewObfConfig,
    phase: 'waiting',
    is_running: false,
    active_action: '',
    prepared_exists: false,
    prepared_at: null,
    approval: null,
    control: null,
    state_path: 'Local OBF API unavailable — no execution data shown',
    journal_path: '',
    summary: { rows: [], planned: 0, target: 0, sent: 0, reconciled: 0, requires_email: 0, failed: 0, pending_sync: 0, timed_out: 0, resolved: 0, retries: 0, start_time: null, last_event_time: null },
    issues: [],
    checkpoints: {},
    history: [],
    last_updated: null
  });

  const previewLeadPrepDashboard = () => ({
    config: previewLeadPrepConfig,
    archive: {
      enabled: true,
      total: 269,
      available: 269,
      consumed: 0,
      updated_at: '2026-07-29T18:06:56',
      path: 'state/lead_exec_research/research_archive/index.json'
    },
    run: {
      exists: true,
      is_today: false,
      run_id: '20260724_140256',
      created_at: '2026-07-24T14:02:56',
      status: 'archived_unreviewed_migration',
      file: 'state/lead_exec_research/runs/20260724_140256.json',
      selected_count: 24,
      fresh_target: previewLeadPrepConfig.base_volume,
      fresh_count: 0,
      archive_match_count: 0,
      conflict_count: 0,
      top_up_count: 0,
      top_up_suppressed_count: 0,
      top_ups_enabled: previewLeadPrepConfig.fresh_volume_top_up_mode !== 'off',
      target_met: false,
      settled_for_day: false,
      source_exhausted: false,
      enabled: true,
      waves: [],
      errors: []
    },
    review: {
      date: new Date().toISOString().slice(0, 10),
      review_complete: false,
      prepared_count: 0,
      approved_count: 0,
      case_study_worthy_count: 0,
      archive_match_count: 0,
      conflict_count: 0,
      fresh_count: 0,
      manual_research_completed: 0,
      leads: [],
      status: 'waiting_for_group',
      window: new Date().getHours() < 18 ? 'first' : new Date().getHours() < 22 ? 'fallback' : 'closed',
      first_deadline_at: `${new Date().toISOString().slice(0, 10)}T18:00:00`,
      fallback_deadline_at: `${new Date().toISOString().slice(0, 10)}T22:00:00`,
      next_deadline_at: `${new Date().toISOString().slice(0, 10)}T${new Date().getHours() < 18 ? '18' : '22'}:00:00`,
      cache: {
        path: 'state/lead_exec_research/dashboard_cache.json',
        cached_at: '2026-07-29T21:29:51',
        cache_date: '2026-07-29',
        checkpoint: {}
      }
    },
    historical: {
      total_leads: 600,
      total_case_study_worthy: 132,
      total_approved: 354,
      date_groups: 12,
      reviewed_date_groups: 0,
      recent_days: []
    },
    watcher: {
      status: previewLeadPrepConfig.autonomous_prep_enabled ? 'waiting' : 'disabled',
      due: previewLeadPrepConfig.prep_time,
      completed_at: null,
      error: ''
    },
    is_running: previewLeadPrepRunning,
    active_action: previewLeadPrepRunning ? 'prepare-review' : '',
    last_updated: '2026-07-29T18:06:56'
  });

  const emit = (channel, ...args) => {
    (listeners.get(channel) || []).forEach(listener => listener({}, ...args));
  };

  const invokeLocalApi = async (channel, value) => {
    const response = await fetch('/api/invoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel, value: value || {} })
    });
    if (!response.ok) throw new Error(`Local dashboard API returned ${response.status}.`);
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error || 'Local dashboard API failed.');
    return payload.result;
  };

  const previewStats = () => ({
    today: new Date().toISOString().slice(0, 10),
    activity_prep_done: false,
    activity_run_done: false,
    followup_prep_done: false,
    followup_daytime_done: false,
    followup_evening_done: false,
    withdrawal_prep_done: false,
    withdrawal_execute_done: false,
    activity: {
      completed: 0,
      prepared: 0,
      is_running: false,
      start_time: null,
      last_event_time: null,
      next_action_due: null,
      list: []
    },
    followups: {
      completed: 0,
      prepared: 0,
      is_running: false,
      start_time: null,
      last_event_time: null,
      next_action_due: null,
      list: []
    },
    withdrawals: {
      completed: 0,
      prepared: 0,
      is_running: false,
      start_time: null,
      last_event_time: null,
      cooldown_active: false,
      cooldown_until: null,
      list: []
    }
  });

  return {
    on(channel, listener) {
      if (!listeners.has(channel)) listeners.set(channel, []);
      listeners.get(channel).push(listener);
    },
    send(channel, payload) {
      if (channel === 'run-command') {
        const isActivityCheck = payload?.scriptPath?.endsWith('scripts/check_prefinal_activity.py');
        if (isActivityCheck) {
          const mode = payload.args?.includes('--prepare-only')
            ? 'prepare-only'
            : payload.args?.includes('--activity-only')
              ? 'activity-only'
              : 'full';
          invokeLocalApi('run-activity-watcher', { mode })
            .then(result => {
              emit('command-output', result.ok
                ? `[Dashboard] Activity Check started through watcher (${mode}).`
                : `[Dashboard] ${result.error || 'Activity Check was not started.'}`);
              emit('command-exit', result.ok ? 0 : 1);
            })
            .catch(error => {
              emit('command-output', `[Dashboard] Could not start Activity Check: ${error.message}`);
              emit('command-exit', 1);
            });
          return;
        }
        const isObf = payload?.scriptPath?.endsWith('helpers/linkedin_outreach_session.py')
          || payload?.scriptPath?.endsWith('scripts/run_outreach_lanes.py');
        const isLeadPrep = payload?.scriptPath?.endsWith('scripts/lead_exec_research.py');
        if (isObf) {
          const isPrepare = payload.args?.[0] === 'prepare-8_30-session';
          invokeLocalApi(isPrepare ? 'start-obf-prepare' : 'start-obf-execute', {
            max_sends: Number(payload.args?.[payload.args.indexOf('--max-sends') + 1] || 30)
          })
            .then(result => {
              emit('command-output', result.ok
                ? `[Dashboard] OBF ${isPrepare ? 'Prep' : 'Execute'} started.`
                : `[Dashboard] ${result.error || 'OBF action was not started.'}`);
              emit('command-exit', result.ok ? 0 : 1);
            })
            .catch(error => {
              emit('command-output', `[Dashboard] Could not start OBF: ${error.message}`);
              emit('command-exit', 1);
            });
          return;
        }
        if (isLeadPrep) previewLeadPrepRunning = true;
        setTimeout(() => {
          emit('command-output', '[Preview mode] Automation execution is disabled in the browser preview.');
          if (isObf) {
            previewObfRunning = false;
            previewObfPhase = payload.args?.[0] === 'prepare-8_30-session' ? 'prepared' : 'completed';
          }
          if (isLeadPrep) previewLeadPrepRunning = false;
          emit('command-exit', 0);
        }, 350);
      } else if (channel === 'stop-command') {
        setTimeout(() => emit('command-stopped'), 100);
      } else if (channel === 'toggle-drawer') {
        document.getElementById('appContainer')?.classList.toggle('open');
      }
    },
    async invoke(channel, value) {
      if ([
        'read-obf-dashboard',
        'refresh-obf-source-cache',
        'start-obf-prepare',
        'start-obf-execute',
        'start-withdrawal-prepare',
        'start-withdrawal-execute',
        'start-withdrawal-full',
        'stop-withdrawal',
        'read-lead-prep-dashboard',
        'read-post-engagement-dashboard',
        'add-post-engagement-source',
        'save-post-engagement-settings',
        'start-post-engagement-dry-run',
        'start-post-engagement-run',
        'pause-post-engagement-run',
        'resume-post-engagement-run',
        'read-job-discovery-dashboard',
        'save-job-discovery-settings',
        'toggle-job-discovery-scheduler',
        'start-job-discovery-run',
        'start-job-discovery-reverify',
        'read-stats',
        'launch-chrome',
        'check-watcher-status',
        'toggle-watcher',
        'refresh-lead-review-cache',
        'save-lead-research-progress',
        'update-lead-review-approval',
        'update-lead-review-use',
        'update-lead-review-complete',
        'sync-lead-review-group',
        'save-manual-lead-research',
        'bridge-manual-lead-processing',
        'save-lead-prep-settings'
      ].includes(channel)) {
        try {
          return await invokeLocalApi(channel, value);
        } catch (error) {
          console.warn(`Falling back to browser preview for ${channel}:`, error);
        }
      }
      if (channel === 'read-stats') return previewStats();
      if (channel === 'read-history') return [];
      if (channel === 'read-obf-dashboard') return unavailableObfDashboard();
      if (channel === 'read-lead-prep-dashboard') return previewLeadPrepDashboard();
      if (channel === 'read-job-discovery-dashboard') return {
        available: false,
        root: 'Daily Job Discovery service',
        error: 'Start the local dashboard server to connect Job Discovery controls.',
        config: {}, scheduler_enabled: false, is_running: false, status: {}
      };
      if (channel === 'refresh-lead-review-cache') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'save-lead-research-progress') return {
        ok: true,
        progress: { status: value.status, notes: value.notes || '', updated_at: new Date().toISOString() }
      };
      if (channel === 'update-lead-review-approval') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'update-lead-review-complete') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'sync-lead-review-group') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'save-manual-lead-research') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'bridge-manual-lead-processing') return { ok: true, dashboard: previewLeadPrepDashboard() };
      if (channel === 'save-obf-settings') {
        previewObfConfig = {
          ...previewObfConfig,
          enabled: Boolean(value.enabled),
          prep_time: value.prep_time,
          exec_time: value.exec_time,
          max_sends: Number(value.max_sends),
          daily_volume: Number(value.daily_volume || previewObfConfig.daily_volume),
          prospects_start_row: Number(value.prospects_start_row || previewObfConfig.prospects_start_row)
        };
        if (value.update_sheet !== false) {
          previewObfVolume = Number(value.daily_volume || previewObfVolume);
          previewObfStartRow = Number(value.prospects_start_row || previewObfStartRow);
        }
        previewWatcherActive = previewWatcherActive || previewObfConfig.enabled;
        return { ok: true, config: previewObfConfig, requires_reprep: value.update_sheet !== false };
      }
      if (channel === 'save-lead-prep-settings') {
        previewLeadPrepConfig = {
          ...previewLeadPrepConfig,
          autonomous_prep_enabled: Boolean(value.autonomous_prep_enabled),
          prep_time: value.prep_time,
          base_volume: Number(value.base_volume || previewLeadPrepConfig.base_volume),
          processing_batch_size: 30,
          approval_gate_enabled: Boolean(value.approval_gate_enabled),
          overlap_scan_mode: value.overlap_scan_mode === 'off' ? 'off' : 'auto',
          fresh_volume_top_up_mode: value.fresh_volume_top_up_mode === 'off' ? 'off' : 'auto'
        };
        return { ok: true, config: previewLeadPrepConfig };
      }
      if (channel === 'check-watcher-status') return previewWatcherActive;
      if (channel === 'toggle-watcher') {
        previewWatcherActive = Boolean(value);
        return previewWatcherActive;
      }
      return null;
    }
  };
}

const ipcRenderer = isElectronRuntime ? require('electron').ipcRenderer : createBrowserPreviewIpc();

// UI Elements
const appContainer = document.getElementById('appContainer');
const filterPills = document.querySelectorAll('.filter-pill');
const categorySections = document.querySelectorAll('.category-section');
const categoryHeaders = document.querySelectorAll('.category-header');
const progressFraction = document.getElementById('progressFraction');
const progressFill = document.getElementById('progressFill');
const closeAppBtn = document.getElementById('closeAppBtn');
const fullScreenBtn = document.getElementById('fullScreenBtn');
const navItems = document.querySelectorAll('.nav-item');
const pageViews = document.querySelectorAll('.page-view');
const pageEyebrow = document.getElementById('pageEyebrow');
const pageTitle = document.getElementById('pageTitle');
const pageDescription = document.getElementById('pageDescription');
const todayDate = document.getElementById('todayDate');
const leadInnerTabs = Array.from(document.querySelectorAll('[data-lead-tab]'));
const leadTabPanels = Array.from(document.querySelectorAll('[data-lead-panel]'));

function formatHeaderDate(value = new Date(), prefix = '') {
  const formatted = new Intl.DateTimeFormat('en', {
    weekday: 'short',
    day: 'numeric',
    month: 'short'
  }).format(value);
  return prefix ? `${prefix} · ${formatted}` : formatted;
}

const pageMetadata = {
  overview: {
    eyebrow: 'Operations workspace',
    title: 'Overview',
    description: "Monitor today's operating flow and move directly to the work that needs attention."
  },
  obf: {
    eyebrow: 'Preparation and execution',
    title: 'OBF',
    description: 'Prepare, execute, and reconcile the daily OBF workload from one operating view.'
  },
  'lead-prep': {
    eyebrow: 'Review to outreach',
    title: 'Lead Prep',
    description: 'Review, research, check activity, rank, and bridge leads into the outreach pipeline.'
  },
  'post-engagement': {
    eyebrow: 'Audience development',
    title: 'Post Engagement',
    description: 'Turn high-engagement source posts into measured likes, follows, and qualified connections.'
  },
  'job-discovery': {
    eyebrow: 'Opportunity sourcing',
    title: 'Job Discovery',
    description: 'Control the daily search service and monitor verified job discovery from its local source of truth.'
  },
  outreach: {
    eyebrow: 'Pipeline operations',
    title: 'Outreach Management',
    description: 'Manage follow-ups, withdrawals, and qualification work without leaving the control center.'
  },
  tasks: {
    eyebrow: 'Execution record',
    title: 'Task Management',
    description: 'Track operating checkpoints and inspect the history of completed automation runs.'
  },
  'lead-engine': {
    eyebrow: 'System pulse',
    title: 'Lead Engine',
    description: 'See the lead system’s essential progress, health, bottlenecks, and exceptions.'
  },
  system: {
    eyebrow: 'Local runtime',
    title: 'System',
    description: 'Control the scheduler, browser profile, keep-awake service, and operating windows.'
  }
};

const selectedPageStorageKey = 'operations-dashboard.selected-page';

function showPage(pageName) {
  if (!Object.prototype.hasOwnProperty.call(pageMetadata, pageName)) return;
  try {
    localStorage.setItem(selectedPageStorageKey, pageName);
  } catch (_) {
    // Navigation remains available when browser storage is disabled.
  }

  navItems.forEach(item => item.classList.toggle('active', item.dataset.page === pageName));
  pageViews.forEach(view => view.classList.toggle('active', view.dataset.page === pageName));

  if (pageEyebrow) pageEyebrow.innerText = pageMetadata[pageName].eyebrow;
  if (pageTitle) pageTitle.innerText = pageMetadata[pageName].title;
  if (pageDescription) pageDescription.innerText = pageMetadata[pageName].description;
  if (pageName !== 'lead-prep' && todayDate) todayDate.innerText = formatHeaderDate();

  if (pageName === 'tasks') loadHistory();
  if (pageName === 'obf') refreshObfDashboard();
  if (pageName === 'lead-prep') refreshLeadPrepDashboard();
  if (pageName === 'post-engagement') refreshPostEngagementDashboard();
  if (pageName === 'job-discovery') refreshJobDiscoveryDashboard();
  if (pageName === 'outreach') refreshStats();
}

navItems.forEach(item => {
  item.addEventListener('click', () => showPage(item.dataset.page));
});
document.querySelectorAll('[data-overview-page]').forEach(button => {
  button.addEventListener('click', () => showPage(button.dataset.overviewPage));
});
document.getElementById('overviewCaffeinateBtn')?.addEventListener('click', () => {
  const overviewDuration = document.getElementById('overviewCaffeinateDuration');
  const systemDuration = document.getElementById('caffeinateDuration');
  if (overviewDuration && systemDuration) systemDuration.value = overviewDuration.value;
  document.getElementById('caffeinateBtn')?.click();
});
document.getElementById('overviewChromeBtn')?.addEventListener('click', () => {
  const target = document.getElementById('overviewChromeTarget')?.value || 'both';
  appendToConsole(`>>> Launching ${target === 'both' ? 'both CDP accounts' : target.toUpperCase()}...`, 'system');
  if (isElectronRuntime) ipcRenderer.send('launch-chrome', target);
  else ipcRenderer.invoke('launch-chrome', { target }).catch(error => appendToConsole(`>>> Chrome launch failed: ${error.message}`, 'error'));
});
function showLeadTab(tabName, focusTab = false) {
  const activeTab = leadInnerTabs.find(tab => tab.dataset.leadTab === tabName);
  if (!activeTab) return;
  leadInnerTabs.forEach(tab => {
    const selected = tab === activeTab;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  leadTabPanels.forEach(panel => {
    const selected = panel.dataset.leadPanel === tabName;
    panel.classList.toggle('active', selected);
    panel.hidden = !selected;
  });
  if (focusTab) activeTab.focus();
}

leadInnerTabs.forEach((tab, index) => {
  tab.addEventListener('click', () => showLeadTab(tab.dataset.leadTab));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + leadInnerTabs.length) % leadInnerTabs.length;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % leadInnerTabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = leadInnerTabs.length - 1;
    showLeadTab(leadInnerTabs[nextIndex].dataset.leadTab, true);
  });
});
showLeadTab('review');

let currentObfDashboard = null;

function escapeHtmlSafe(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatObfTime(value, includeDate = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('en', includeDate
    ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
    : { hour: '2-digit', minute: '2-digit', second: '2-digit' }
  ).format(date);
}

function obfPhasePresentation(phase) {
  const map = {
    source_ready: ['Test source selected', 'Showing the isolated test queue. Prepare it before any execution test.'],
    waiting: ['Waiting for preparation', 'The scheduled preparation will freeze today’s approved queue and runtime plan.'],
    preparing: ['Preparing today’s OBF state', 'Validating approval, queue quality, reporting fields, and runtime slots.'],
    prepared: ['Prepared and ready to execute', 'Today’s local state is frozen. Execution can begin from this exact queue.'],
    executing: ['Executing the prepared queue', 'Live outcomes are updating from the local execution journal.'],
    completed: ['Today’s OBF run is complete', 'The prepared queue has been processed and reporting outcomes reconciled.'],
    failed: ['OBF requires attention', 'A checkpoint failed or stopped. Review the issue report before retrying.']
  };
  return map[phase] || map.waiting;
}

function updateObfPhases(phase) {
  const order = ['source', 'prepare', 'execute', 'reconcile'];
  let activeIndex = 0;
  let doneThrough = -1;
  if (phase === 'preparing') { activeIndex = 1; doneThrough = 0; }
  if (phase === 'prepared') { activeIndex = 2; doneThrough = 1; }
  if (phase === 'executing') { activeIndex = 2; doneThrough = 1; }
  if (phase === 'completed') { activeIndex = 3; doneThrough = 3; }
  if (phase === 'failed') { activeIndex = 2; doneThrough = 1; }
  document.querySelectorAll('#obfPhaseTrack .obf-phase').forEach((element, index) => {
    element.classList.toggle('active', index === activeIndex && phase !== 'completed');
    element.classList.toggle('done', index <= doneThrough);
  });
}

function renderObfRows() {
  const body = document.getElementById('obfStateBody');
  if (!body || !currentObfDashboard) return;
  const search = String(document.getElementById('obfTableSearch')?.value || '').trim().toLowerCase();
  const filter = document.getElementById('obfStatusFilter')?.value || 'all';
  const issueStates = ['Failed', 'Timed Out', 'Sync Pending', 'Requires Email'];
  const rows = (currentObfDashboard.summary?.rows || []).filter(row => {
    const haystack = `${row.company} ${row.contact_name} ${row.contact_title} ${row.id}`.toLowerCase();
    const matchesSearch = !search || haystack.includes(search);
    const matchesFilter = filter === 'all'
      || row.progress === filter
      || (filter === 'issue' && issueStates.includes(row.progress));
    return matchesSearch && matchesFilter;
  });
  const count = document.getElementById('obfTableCount');
  if (count) count.innerText = `${rows.length} of ${currentObfDashboard.summary?.rows?.length || 0} rows`;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7"><div class="empty-state">${currentObfDashboard.prepared_exists ? 'No rows match this view.' : 'No prepared OBF state exists for today. Run Prep to build it.'}</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map(row => `
    <tr>
      <td class="obf-row-index">${row.index}</td>
      <td><div class="obf-lead-cell"><strong title="${escapeHtmlSafe(row.company)}">${escapeHtmlSafe(row.company)}</strong><span>ID ${escapeHtmlSafe(row.id)} · Sheet row ${escapeHtmlSafe(row.prospect_row)}</span></div></td>
      <td><div class="obf-contact-cell">${row.contact_linkedin ? `<a href="${escapeHtmlSafe(row.contact_linkedin)}" target="_blank" rel="noreferrer">` : ''}<strong title="${escapeHtmlSafe(row.contact_name)}">${escapeHtmlSafe(row.contact_name)}</strong>${row.contact_linkedin ? '</a>' : ''}<span>${escapeHtmlSafe(row.contact_title || 'Title unavailable')}</span></div></td>
      <td><div class="obf-runtime-cell"><strong>${escapeHtmlSafe(row.primary_lane || 'Unassigned')}</strong><span>${escapeHtmlSafe(String(row.worker_id || 'Unassigned').replace('_account', ' CDP'))}</span></div></td>
      <td><div class="obf-runtime-cell"><strong>Slot ${escapeHtmlSafe(row.slot_id)} · ${escapeHtmlSafe(row.delay_sec ?? '—')}s</strong><span>${escapeHtmlSafe((row.activity_timing || 'timing n/a').replace('_', ' '))}${row.diversion && row.diversion !== 'none' ? ` · ${escapeHtmlSafe(row.diversion)}` : ''}</span></div></td>
      <td class="obf-progress-cell"><span class="obf-status-pill ${escapeHtmlSafe(row.tone)}">${escapeHtmlSafe(row.progress)}</span>${row.retry_count ? `<span class="obf-row-retry">${row.retry_count} retry signal${row.retry_count === 1 ? '' : 's'}</span>` : ''}</td>
      <td><div class="obf-update-cell"><strong>${formatObfTime(row.last_update)}</strong><span title="${escapeHtmlSafe(row.detail || row.latest_stage)}">${escapeHtmlSafe(row.detail || titleCaseStatusClient(row.latest_stage) || 'No execution event')}</span></div></td>
    </tr>
  `).join('');
}

function renderObfSourceRows() {
  const body = document.getElementById('obfSourceBody');
  if (!body || !currentObfDashboard) return;
  const source = currentObfDashboard.source_summary || {};
  document.getElementById('obfSourceTotal').innerText = source.total || 0;
  document.getElementById('obfSourceFresh').innerText = source.fresh || 0;
  document.getElementById('obfSourceAvailable').innerText = source.available || 0;
  document.getElementById('obfSourceNeedsLane').innerText = source.needs_lane_assignment || 0;
  const filter = document.getElementById('obfSourceFilter')?.value || 'available';
  const rows = (currentObfDashboard.source_rows || []).filter(row => (
    filter === 'all' || (filter === 'fresh' && row.fresh) || (filter === 'available' && row.available) || (filter === 'needs_lane' && row.available && !row.lane_assigned)
  ));
  document.getElementById('obfSourceCount').innerText = `${rows.length} of ${source.total || 0} rows`;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5"><div class="empty-state">No prospects match this source filter.</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(row => `
    <tr>
      <td class="obf-row-index">${row.index}</td>
      <td><div class="obf-lead-cell"><strong>${escapeHtmlSafe(row.company || 'Company unavailable')}</strong><span>ID ${escapeHtmlSafe(row.id)}</span></div></td>
      <td><div class="obf-contact-cell"><strong>${escapeHtmlSafe(row.contact_name || 'Contact unavailable')}</strong><span>${escapeHtmlSafe(row.contact_title || 'Title unavailable')}</span></div></td>
      <td><span class="obf-status-pill ${row.lane_assigned ? 'neutral' : 'warning'}">${escapeHtmlSafe(row.primary_lane || 'Unassigned')}</span></td>
      <td><span class="obf-status-pill ${row.available && row.lane_assigned ? 'success' : (row.available ? 'warning' : 'neutral')}">${escapeHtmlSafe(row.source_state)}</span></td>
    </tr>
  `).join('');
}

function titleCaseStatusClient(value) {
  return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
}

function renderObfIssues(issues) {
  const list = document.getElementById('obfIssueList');
  const badge = document.getElementById('obfIssueBadge');
  if (!list || !badge) return;
  badge.className = `obf-status-pill ${issues.length ? 'warning' : 'success'}`;
  badge.innerText = issues.length ? `${issues.length} open` : 'Clear';
  if (!issues.length) {
    list.innerHTML = '<div class="empty-state">No failures, errors, retries, or pending syncs reported for today.</div>';
    return;
  }
  list.innerHTML = issues.map(issue => `
    <div class="obf-issue-item ${escapeHtmlSafe(issue.severity)}">
      <i></i>
      <div><strong>${escapeHtmlSafe(issue.title)}</strong><span>${escapeHtmlSafe(issue.detail)}</span></div>
      <time>${formatObfTime(issue.time)}</time>
    </div>
  `).join('');
}

function renderObfHistory(history) {
  const body = document.getElementById('obfHistoryBody');
  if (!body) return;
  if (!history?.length) {
    body.innerHTML = '<tr><td colspan="8"><div class="empty-state">No historical OBF states found.</div></td></tr>';
    return;
  }
  body.innerHTML = history.map(record => `
    <tr>
      <td>${escapeHtmlSafe(new Intl.DateTimeFormat('en', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${record.date}T12:00:00`)))}</td>
      <td>${record.prepared}</td>
      <td>${record.sent}</td>
      <td>${record.reconciled}</td>
      <td><span class="obf-status-pill ${record.issues ? 'warning' : 'success'}">${record.issues}</span></td>
      <td>${escapeHtmlSafe(record.issue_summary || '—')}</td>
      <td><div class="obf-history-meter" style="--completion:${Math.max(0, Math.min(100, record.completion))}%"><i></i><span>${record.completion}%</span></div></td>
      <td>${formatObfTime(record.started_at)} – ${formatObfTime(record.completed_at)}</td>
    </tr>
  `).join('');
}

function renderObfDashboard(data) {
  currentObfDashboard = data;
  const summary = data.summary || {};
  const [phaseTitle, phaseDetail] = obfPhasePresentation(data.phase);
  document.getElementById('obfSequenceStatus').innerText = phaseTitle;
  document.getElementById('obfSequenceDetail').innerText = phaseDetail;
  document.getElementById('obfSequenceFraction').innerText = `${summary.resolved || 0} / ${summary.planned || 0}`;
  document.getElementById('obfSequenceUpdated').innerText = data.preview
    ? `Preview data · ${formatObfTime(data.last_updated)}`
    : (data.last_updated ? `Updated ${formatObfTime(data.last_updated)}` : 'No state today');
  document.getElementById('obfSequenceFill').style.width = `${summary.planned ? Math.min(100, Math.round(((summary.resolved || 0) / summary.planned) * 100)) : 0}%`;
  document.getElementById('obfLiveDot').classList.toggle('running', Boolean(data.is_running));
  updateObfPhases(data.phase);

  const config = data.config || {};
  const autonomyToggle = document.getElementById('obfAutonomyToggle');
  autonomyToggle.setAttribute('aria-checked', String(Boolean(config.enabled)));
  document.getElementById('obfAutonomyDetail').innerText = config.enabled
    ? `Enabled · prep ${config.prep_time}, exec ${config.exec_time}`
    : 'Disabled · manual runs only';
  const editingSettings = Boolean(document.activeElement?.closest?.('.obf-settings-grid'));
  if (!editingSettings) {
    document.getElementById('obfDailyVolume').value = data.control?.effective_target || summary.target || config.daily_volume || 30;
    document.getElementById('obfStartRow').value = data.control?.prospects_start_row || config.prospects_start_row || '';
    document.getElementById('obfPrepTime').value = config.prep_time || '08:25';
    document.getElementById('obfExecTime').value = config.exec_time || '08:30';
    document.getElementById('obfMaxSends').value = config.max_sends || 30;
  }
  document.getElementById('obfPrepPhaseTime').innerText = `${config.prep_time || '08:25'} · freeze state`;
  document.getElementById('obfExecPhaseTime').innerText = `${config.exec_time || '08:30'} · process queue`;

  const approvalBadge = document.getElementById('obfApprovalBadge');
  approvalBadge.className = `obf-status-pill ${data.approval?.approved ? 'success' : (data.approval ? 'danger' : 'neutral')}`;
  approvalBadge.innerText = data.approval?.approved ? 'Approved' : (data.approval ? 'Not approved' : 'Approval unknown');
  const runApproved = document.getElementById('obfRunApproved');
  if (runApproved && document.activeElement !== runApproved) runApproved.checked = Boolean(data.approval?.approved);
  const controlStatus = document.getElementById('obfControlStatus');
  if (controlStatus) controlStatus.innerText = data.approval
    ? `${data.approval.approved ? 'Approved' : 'Blocked'} · ${data.approval.status || 'No status'}`
    : 'Refresh snapshot to load today’s control row';

  const isUnpreparedSource = !data.prepared_exists;
  const isUnpreparedTestSource = data.source_mode === 'test' && isUnpreparedSource;
  document.getElementById('obfPreparedLabel').innerText = isUnpreparedSource
    ? (isUnpreparedTestSource ? 'Test source available' : 'Prospect source available')
    : 'Prepared';
  document.getElementById('obfStateHeading').innerText = 'Prepared queue and live outcomes';
  document.getElementById('obfPreparedMetric').innerText = summary.planned || 0;
  document.getElementById('obfPreparedDetail').innerText = isUnpreparedSource
    ? 'available · not frozen'
    : 'rows in frozen state';
  document.getElementById('obfSentMetric').innerText = summary.sent || 0;
  document.getElementById('obfReconciledMetric').innerText = summary.reconciled || 0;
  const attentionCount = (summary.failed || 0) + (summary.pending_sync || 0) + (summary.timed_out || 0) + (summary.requires_email || 0);
  document.getElementById('obfIssueMetric').innerText = attentionCount;
  document.getElementById('obfIssueDetail').innerText = `${summary.retries || 0} retry signals · ${summary.pending_sync || 0} sync pending`;

  document.getElementById('obfPreparedAt').innerText = formatObfTime(data.prepared_at, true);
  document.getElementById('obfStartedAt').innerText = formatObfTime(summary.start_time, true);
  document.getElementById('obfLastEvent').innerText = formatObfTime(summary.last_event_time, true);
  document.getElementById('obfRequiresEmail').innerText = summary.requires_email || 0;
  document.getElementById('obfPendingSync').innerText = summary.pending_sync || 0;
  document.getElementById('obfRetries').innerText = summary.retries || 0;
  document.getElementById('obfStatePath').innerText = data.state_path || '';

  const prepButton = document.getElementById('obfPrepBtn');
  const execButton = document.getElementById('obfExecBtn');
  prepButton.disabled = Boolean(data.is_running);
  execButton.disabled = Boolean(data.is_running || !data.prepared_exists);
  prepButton.classList.toggle('running', data.active_action === 'prepare');
  execButton.classList.toggle('running', data.active_action === 'execute');

  renderObfRows();
  renderObfSourceRows();
  renderObfIssues(data.issues || []);
  renderObfHistory(data.history || []);
  if (currentOverviewStats) {
    const summary = data.summary || {};
    const successes = Number(summary.sent || 0) + Number(summary.reconciled || 0);
    const resolved = successes + Number(summary.requires_email || 0) + Number(summary.failed || 0) + Number(summary.timed_out || 0);
    const value = document.getElementById('overviewOutreachSuccessValue');
    const detail = document.getElementById('overviewOutreachSuccessDetail');
    if (value) value.innerText = resolved ? `${Math.round((successes / resolved) * 100)}%` : '—';
    if (detail) detail.innerText = resolved ? `${successes} successful of ${resolved} resolved` : 'no resolved outreach today';
    renderOverviewWorkflow(currentOverviewStats);
  }
}

async function refreshObfDashboard() {
  const data = await ipcRenderer.invoke('read-obf-dashboard');
  if (data) renderObfDashboard(data);
}

function readObfSettingsForm(enabledOverride = null, updateSheet = true) {
  const toggle = document.getElementById('obfAutonomyToggle');
  return {
    enabled: enabledOverride === null ? toggle.getAttribute('aria-checked') === 'true' : enabledOverride,
    daily_volume: Number(document.getElementById('obfDailyVolume').value),
    prospects_start_row: Number(document.getElementById('obfStartRow').value || 0),
    prep_time: document.getElementById('obfPrepTime').value,
    exec_time: document.getElementById('obfExecTime').value,
    max_sends: Number(document.getElementById('obfMaxSends').value),
    approved: Boolean(document.getElementById('obfRunApproved')?.checked),
    update_sheet: updateSheet
  };
}

async function saveObfSettings(enabledOverride = null, updateSheet = true) {
  const feedback = document.getElementById('obfSettingsFeedback');
  const saveButton = document.getElementById('obfSaveSettings');
  const settings = readObfSettingsForm(enabledOverride, updateSheet);
  if (!settings.daily_volume || settings.daily_volume < 1 || settings.daily_volume > 30) {
    feedback.innerText = 'Daily volume must be between 1 and 30.';
    return false;
  }
  saveButton.disabled = true;
  feedback.innerText = 'Saving OBF settings…';
  const result = await ipcRenderer.invoke('save-obf-settings', settings);
  saveButton.disabled = false;
  if (!result?.ok) {
    feedback.innerText = result?.error || 'Could not save OBF settings.';
    await refreshObfDashboard();
    return false;
  }
  feedback.innerText = result.requires_reprep
    ? 'Settings saved. Run Prep again so today’s frozen state uses the changes.'
    : 'Settings saved. The local scheduler will use these values.';
  await refreshObfDashboard();
  return true;
}

document.getElementById('obfTableSearch')?.addEventListener('input', renderObfRows);
document.getElementById('obfStatusFilter')?.addEventListener('change', renderObfRows);
document.getElementById('obfSourceFilter')?.addEventListener('change', renderObfSourceRows);
document.getElementById('obfLocalStatePanel')?.after(document.getElementById('obfSourcePanel'));
document.getElementById('obfRefreshSourceBtn')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await ipcRenderer.invoke('refresh-obf-source-cache');
    if (result?.dashboard) renderObfDashboard(result.dashboard);
  } finally {
    button.disabled = false;
  }
});
document.getElementById('obfSaveSettings')?.addEventListener('click', () => saveObfSettings());
document.getElementById('obfAutonomyToggle')?.addEventListener('click', async event => {
  const enabled = event.currentTarget.getAttribute('aria-checked') !== 'true';
  event.currentTarget.setAttribute('aria-checked', String(enabled));
  await saveObfSettings(enabled, false);
});

let currentLeadPrepDashboard = null;
let leadReviewDraftKey = '';
let leadProcessingRenderKey = '';
const leadReviewDraft = new Map();
const leadResearchAutosave = new Map();

function initializeLeadReviewDraft(review) {
  const key = `${review?.date || ''}:${review?.group_row || 0}`;
  if (key === leadReviewDraftKey) return;
  leadReviewDraftKey = key;
  leadReviewDraft.clear();
  (review?.leads || []).forEach(lead => {
    leadReviewDraft.set(String(lead.run_id || ''), {
      row: Number(lead.row || 0),
      run_id: String(lead.run_id || ''),
      expected_approved: Boolean(lead.approved),
      expected_use: String(lead.use || ''),
      approved: Boolean(lead.approved),
      use: String(lead.use || ''),
      dirty: false
    });
  });
}

function draftDecision(lead) {
  return leadReviewDraft.get(String(lead.run_id || '')) || {
    approved: Boolean(lead.approved),
    use: String(lead.use || ''),
    dirty: false
  };
}

function updateLeadDraftSummary() {
  const decisions = [...leadReviewDraft.values()];
  const dirty = decisions.filter(item => item.dirty).length;
  const approved = decisions.filter(item => item.approved).length;
  const draftCount = document.getElementById('leadReviewDraftCount');
  const approvedCount = document.getElementById('leadReviewApprovedCount');
  const button = document.getElementById('leadReviewHandoffBtn');
  if (draftCount) draftCount.innerText = dirty;
  if (approvedCount) approvedCount.innerText = approved;
  if (button) {
    const review = currentLeadPrepDashboard?.review || {};
    const complete = Boolean(review.review_complete);
    button.disabled = !review.group_row || !decisions.length || complete;
    button.classList.toggle('complete', complete);
    const small = button.querySelector('small');
    const strong = button.querySelector('strong');
    if (complete) {
      if (small) small.innerText = 'Sheet synced';
      if (strong) strong.innerText = 'Review complete';
    } else {
      if (small) small.innerText = 'Save review';
      if (strong) strong.innerText = 'Sync & continue';
    }
  }
}

function renderLeadPrepWaves(data) {
  const body = document.getElementById('leadPrepWavesBody');
  if (!body) return;
  const waves = data.run?.is_today ? (data.run.waves || []) : [];
  if (!waves.length) {
    body.innerHTML = '<tr><td colspan="7"><div class="empty-state">Run preparation to see overlap-scan waves.</div></td></tr>';
    return;
  }
  let freshTotal = 0;
  const target = Number(data.run.fresh_target || data.config?.base_volume || 0);
  body.innerHTML = waves.map(wave => {
    const matches = Number(wave.archive_matches || 0);
    const conflicts = Number(wave.conflicts || 0);
    const fresh = Math.max(0, Number(wave.selected || 0) - matches - conflicts);
    freshTotal += fresh;
    return `
      <tr>
        <td><strong>${escapeHtmlSafe(wave.label || 'Wave')}</strong></td>
        <td>${Number(wave.requested || 0)}</td>
        <td>${Number(wave.selected || 0)}</td>
        <td><span class="obf-status-pill success">${fresh}</span></td>
        <td>${matches}</td>
        <td><span class="obf-status-pill ${conflicts ? 'warning' : 'neutral'}">${conflicts}</span></td>
        <td>${Math.max(0, target - freshTotal)}</td>
      </tr>
    `;
  }).join('');
}

function formatLeadCountdown(deadlineAt) {
  const remaining = Math.max(0, new Date(deadlineAt).getTime() - Date.now());
  const totalSeconds = Math.floor(remaining / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds].map(value => String(value).padStart(2, '0')).join(':');
}

function formatLeadDeadlineTime(deadlineAt) {
  if (!deadlineAt) return '—';
  return new Intl.DateTimeFormat('en', {
    hour: 'numeric',
    minute: '2-digit'
  }).format(new Date(deadlineAt));
}

function leadReviewStatusCopy(review, approvalGateEnabled = false) {
  const approved = Number(review.approved_count || 0);
  const prepared = Number(review.prepared_count || 0);
  if (!approvalGateEnabled && prepared) {
    return {
      title: 'Approval gate is disabled',
      detail: `${prepared} prepared leads will be processed in their assigned lanes; approval remains optional.`
    };
  }
  if (review.processing_automation?.paused) {
    return {
      title: 'Processing paused for testing',
      detail: `${prepared} prepared leads are available. The external 6:00 PM and 10:00 PM Codex runs are both paused.`
    };
  }
  if (review.status === 'review_complete') {
    return {
      title: 'Today’s review is marked complete',
      detail: `${approved} of ${prepared} prepared leads are approved for the next Codex processing window.`
    };
  }
  if (review.status === 'review_due_before_first_run') {
    return {
      title: 'Manual review due before 6:00 PM',
      detail: 'Approved rows will be picked up by the first Codex Process Approved Leads run.'
    };
  }
  if (review.status === 'first_run_missed') {
    return {
      title: 'First window missed — review before 10:00 PM',
      detail: approved
        ? `${approved} approved rows are ready for the fallback Codex run.`
        : 'Approve rows before 10:00 PM; with no approvals, the fallback processes the full fresh group.'
    };
  }
  if (review.status === 'fallback_due_or_passed') {
    return {
      title: '10:00 PM fallback window reached',
      detail: approved
        ? `${approved} approved rows are available to the Codex flow.`
        : 'No approvals are recorded, so the Codex fallback may process the full fresh group.'
    };
  }
  return {
    title: 'Waiting for today’s prepared group',
    detail: review.window === 'first'
      ? 'The first Codex processing window is at 6:00 PM.'
      : review.window === 'fallback'
        ? 'No cached group yet. The fallback Codex processing window is at 10:00 PM.'
        : 'No Lead Review group was cached for today.'
  };
}

function filteredPreparedLeads(data) {
  const leads = data.review?.leads || [];
  const filter = document.getElementById('leadPreparedFilter')?.value || 'all';
  const query = String(document.getElementById('leadPreparedSearch')?.value || '').trim().toLowerCase();
  return leads.filter(lead => {
    if (query && !`${lead.company || ''} ${lead.website || ''} ${lead.run_id || ''}`.toLowerCase().includes(query)) {
      return false;
    }
    if (filter === 'fresh') return lead.overlap_status === 'Fresh';
    if (filter === 'archive') return lead.overlap_status === 'Archive Match';
    if (filter === 'conflict') return lead.overlap_status === 'Possible Match';
    const decision = draftDecision(lead);
    if (filter === 'approved') return Boolean(decision.approved);
    if (filter === 'case-study') return String(decision.use || '').toLowerCase() === 'case study worthy';
    return true;
  });
}

function renderPreparedLeads(data) {
  const filter = document.getElementById('leadPreparedFilter')?.value || 'all';
  const preparedWrap = document.getElementById('leadPreparedTableWrap');
  const waveWrap = document.getElementById('leadWaveTableWrap');
  const showingWaves = filter === 'waves';
  preparedWrap.hidden = showingWaves;
  waveWrap.hidden = !showingWaves;
  if (showingWaves) {
    renderLeadPrepWaves(data);
    return;
  }

  const body = document.getElementById('leadPreparedBody');
  const leads = filteredPreparedLeads(data);
  if (!leads.length) {
    const hasPrepared = Boolean(data.review?.leads?.length);
    body.innerHTML = `<tr><td colspan="6"><div class="empty-state">${hasPrepared ? 'No prepared leads match this filter.' : 'No prepared group cached for today.'}</div></td></tr>`;
    return;
  }
  body.innerHTML = leads.map(lead => {
    const decision = draftDecision(lead);
    const company = escapeHtmlSafe(lead.company || 'Unnamed company');
    const companyCell = lead.website
      ? `<a href="${escapeHtmlSafe(lead.website)}" target="_blank" rel="noreferrer">${company}</a>`
      : company;
    const use = String(decision.use || '');
    const locked = data.review?.review_complete ? 'disabled' : '';
    return `
      <tr data-lead-id="${escapeHtmlSafe(lead.run_id)}" data-sheet-row="${Number(lead.row || 0)}">
        <td><strong>${companyCell}</strong><small>${escapeHtmlSafe(lead.run_id)}</small></td>
        <td>${escapeHtmlSafe(lead.primary_lane || '—')}</td>
        <td>${escapeHtmlSafe(lead.prep_wave || 'Base')}</td>
        <td><span class="obf-status-pill ${lead.overlap_status === 'Fresh' ? 'success' : lead.overlap_status === 'Possible Match' ? 'warning' : 'neutral'}">${escapeHtmlSafe(lead.overlap_status || 'Fresh')}</span></td>
        <td>
          <select class="lead-use-select" ${locked} aria-label="Use for ${company}">
            <option value="" ${!use ? 'selected' : ''}>Choose use</option>
            <option value="Potential leads" ${use === 'Potential leads' ? 'selected' : ''}>Potential leads</option>
            <option value="Case study worthy" ${use === 'Case study worthy' ? 'selected' : ''}>Case study worthy</option>
          </select>
        </td>
        <td><input class="lead-approval-check" type="checkbox" ${decision.approved ? 'checked' : ''} ${locked} aria-label="Approve ${company}"></td>
      </tr>
    `;
  }).join('');
  updateLeadDraftSummary();
}

function renderLeadFinalReport(data) {
  const report = data.processing?.report || {};
  const counts = report.counts || {};
  const write = report.write || {};
  const leadCount = Number(counts.lead_count || 0);
  const researchCompleted = Number(counts.research_completed || 0);
  const rowsReady = Number(counts.rows_ready || 0);
  const rowsWritten = Number(counts.rows_written || 0);
  const searchTotal = Number(counts.search_total || 0);
  const searchCompleted = Number(counts.search_completed || 0);
  const searchPending = Number(counts.search_pending || 0);
  const issueCount = Number(counts.rows_skipped || 0)
    + Number(counts.search_needs_review || 0)
    + Number(counts.archive_conflicts || 0)
    + Number(counts.errors || 0);
  const outcome = document.getElementById('leadReportOutcome');
  if (!outcome) return;
  outcome.className = `obf-status-pill ${report.tone || 'neutral'}`;
  outcome.innerText = report.outcome_label || 'Waiting for computation';
  document.getElementById('leadReportSummary').innerText = report.summary
    || 'No persisted Process Approved Leads computation matches this review group yet.';
  document.getElementById('leadReportMode').innerText = report.mode_label || 'Not started';
  document.getElementById('leadReportUpdated').innerText = report.last_updated
    ? `Report updated ${formatObfTime(report.last_updated, true)}`
    : 'No final report yet';
  document.getElementById('leadReportResearch').innerText = `${researchCompleted} / ${leadCount}`;
  document.getElementById('leadReportResearchDetail').innerText = leadCount
    ? `P1 ${Number(counts.p1_count || 0)} · P2 ${Number(counts.p2_count || 0)} · P3 ${Number(counts.p3_count || 0)}`
    : 'No computation';
  document.getElementById('leadReportReady').innerText = `${rowsReady} / ${leadCount}`;
  document.getElementById('leadReportReadyDetail').innerText = leadCount
    ? `${Math.max(0, leadCount - rowsReady)} unresolved before write`
    : 'Waiting for results';
  document.getElementById('leadReportSearch').innerText = `${searchCompleted} / ${searchTotal}`;
  document.getElementById('leadReportSearchDetail').innerText = searchTotal
    ? `${searchPending} pending · ${Number(counts.search_needs_review || 0)} need review`
    : 'No search tasks recorded';
  document.getElementById('leadReportWrite').innerText = `${rowsWritten} / ${leadCount}`;
  document.getElementById('leadReportWriteDetail').innerText = write.attempted
    ? `${Number(counts.rows_skipped || 0)} skipped · ${write.verified === true ? 'verified' : (write.verified === false ? 'not verified' : 'verification not recorded')}`
    : 'Not attempted';
  document.getElementById('leadReportIssues').innerText = issueCount;
  document.getElementById('leadReportIssueDetail').innerText = issueCount
    ? `${Number(counts.errors || 0)} errors · ${Number(counts.archive_conflicts || 0)} conflicts`
    : 'No recorded issues';
  document.getElementById('leadReportRetries').innerText = Number(counts.retries || 0);
  document.getElementById('leadReportRetryDetail').innerText = Number(counts.captcha_events || 0)
    ? `${Number(counts.captcha_events)} CAPTCHA events recorded`
    : 'No CAPTCHA events recorded';
  document.getElementById('leadReportDestination').innerText = write.attempted
    ? `${write.destination || 'Pre-final'} · ${write.created_at ? formatObfTime(write.created_at, true) : 'write time unavailable'}${write.queue_fingerprint ? ` · queue ${write.queue_fingerprint}` : ''}`
    : 'No destination write recorded';
  document.getElementById('leadReportRawStatus').innerText = `Local status: ${titleCaseStatusClient(report.raw_status || 'waiting')}`;
  const issueList = document.getElementById('leadReportIssueList');
  const issues = report.issues || [];
  if (!issues.length) {
    issueList.innerHTML = '<div class="empty-state">No final issues reported.</div>';
    return;
  }
  issueList.innerHTML = issues.slice(0, 8).map(issue => {
    const identity = issue.company || issue.lead_id || titleCaseStatusClient(issue.type || 'Issue');
    return `
      <div class="lead-report-issue ${issue.tone === 'danger' ? 'danger' : ''}">
        <i></i>
        <div><strong>${escapeHtmlSafe(identity)}</strong><span>${escapeHtmlSafe(issue.message || 'Issue recorded in local state.')}</span></div>
        <em>${escapeHtmlSafe(issue.lead_id || titleCaseStatusClient(issue.type || 'issue'))}</em>
      </div>
    `;
  }).join('') + (issues.length > 8
    ? `<div class="empty-state">${issues.length - 8} additional issues are retained in the computation report.</div>`
    : '');
}

function renderLeadProcessing(data) {
  const processing = data.processing || {};
  const leads = processing.leads || [];
  const renderKey = JSON.stringify({
    computation_file: processing.computation_file || '',
    computation_status: processing.computation_status || '',
    manual_editable: Boolean(processing.manual_editable),
    manual_ready_count: Number(processing.manual_ready_count || 0),
    bridge_ready: Boolean(processing.bridge_ready),
    bridged: Boolean(processing.bridged),
    review_complete: Boolean(data.review?.review_complete),
    leads: leads.map(lead => ({
      id: lead.run_id,
      status: lead.processing_status,
      executives: lead.executives || [],
      destination: lead.destination_row || {}
    }))
  });
  if (renderKey === leadProcessingRenderKey) return;
  leadProcessingRenderKey = renderKey;
  const body = document.getElementById('leadProcessingBody');
  const state = document.getElementById('leadProcessingState');
  const count = document.getElementById('leadProcessingCount');
  const readyCount = document.getElementById('leadManualReadyCount');
  const bridgeButton = document.getElementById('leadManualBridgeBtn');
  const path = document.getElementById('leadProcessingPath');
  if (!body || !state || !count || !readyCount || !bridgeButton || !path) return;

  count.innerText = `${leads.length} ${leads.length === 1 ? 'lead' : 'leads'}`;
  readyCount.innerText = `${Number(processing.manual_ready_count || 0)} / ${leads.length} ready`;
  state.className = `obf-status-pill ${leads.length ? (processing.computation_file ? 'active' : 'success') : 'neutral'}`;
  state.innerText = leads.length
    ? (processing.computation_status ? titleCaseStatusClient(processing.computation_status) : 'Ready for processing')
    : (data.config?.approval_gate_enabled ? (data.review?.review_complete ? 'Waiting for computation' : 'Waiting for review handoff') : 'Waiting for prepared leads');
  path.innerText = processing.computation_file || 'Manual research state will be created when you save the first row';
  const alreadyBridged = Boolean(processing.bridged);
  bridgeButton.disabled = alreadyBridged || !processing.bridge_ready;
  bridgeButton.classList.toggle('complete', alreadyBridged);
  bridgeButton.querySelector('span').innerText = alreadyBridged
    ? 'Already bridged to Pre-final'
    : 'Bridge completed research';
  bridgeButton.title = alreadyBridged
    ? 'This Codex computation has already been written and verified in Pre-final.'
    : 'Writes completed manual research to Pre-final.';
  bridgeButton.setAttribute('aria-label', bridgeButton.title);

  if (!leads.length) {
    const message = data.review?.review_complete
      ? 'The reviewed group is waiting for Process Approved Leads to create its local computation.'
      : 'Complete today’s review to open its processing queue.';
    body.innerHTML = `<tr><td colspan="17"><div class="empty-state">${message}</div></td></tr>`;
    return;
  }

  body.innerHTML = leads.map(lead => {
    const company = escapeHtmlSafe(lead.company || 'Unnamed company');
    const companyCell = lead.website
      ? `<a href="${escapeHtmlSafe(lead.website)}" target="_blank" rel="noreferrer">${company}</a>`
      : company;
    const destination = lead.destination_row?.row_number
      ? `Pre-final row ${lead.destination_row.row_number}`
      : lead.destination_row?.range || 'Not written';
    const people = [...(lead.executives || [])];
    while (people.length < 3) people.push({});
    const editable = Boolean(processing.manual_editable);
    const personCells = people.slice(0, 3).map((person, personIndex) => {
      if (!editable) {
        const profile = person.linkedin_url
          ? `<a href="${escapeHtmlSafe(person.linkedin_url)}" target="_blank" rel="noreferrer">Open profile</a>`
          : '—';
        return `<td>${escapeHtmlSafe(person.name || '—')}</td><td>${escapeHtmlSafe(person.title || '—')}</td><td>${profile}</td><td>${escapeHtmlSafe(person.email || '—')}</td>`;
      }
      const input = (field, value, placeholder, type = 'text') => (
        `<input class="manual-research-input" type="${type}" data-person-index="${personIndex}" data-person-field="${field}" value="${escapeHtmlSafe(value || '')}" placeholder="${placeholder} · ${(personIndex === 0 && field !== 'email') ? 'required' : 'optional'}" aria-label="P${personIndex + 1} ${placeholder} for ${company}">`
      );
      return [
        `<td>${input('name', person.name, 'Name')}</td>`,
        `<td>${input('title', person.title, 'Title')}</td>`,
        `<td>${input('linkedin_url', person.linkedin_url, 'LinkedIn URL', 'url')}</td>`,
        `<td>${input('email', person.email, 'Email', 'email')}</td>`
      ].join('');
    }).join('');
    const ready = lead.processing_status === 'manual_ready';
    return `
      <tr data-processing-lead-id="${escapeHtmlSafe(lead.run_id)}">
        <td><strong>${companyCell}</strong><small>${escapeHtmlSafe(lead.run_id)}</small></td>
        <td>${escapeHtmlSafe(lead.use || 'Unclassified')}</td>
        <td><span class="obf-status-pill ${ready ? 'success' : (lead.processing_status === 'awaiting_processing' ? 'neutral' : 'active')}">${escapeHtmlSafe(titleCaseStatusClient(lead.processing_status || 'awaiting_processing'))}</span></td>
        ${personCells}
        <td>${escapeHtmlSafe(destination)}</td>
        <td>${editable
          ? `<span class="lead-research-autosave ${ready ? 'saved' : ''}" role="status"><i></i>${ready ? 'Saved locally' : 'Autosaves'}</span>`
          : '<span class="obf-status-pill success">Locked</span>'}</td>
      </tr>
    `;
  }).join('');
}

function renderLeadActivity(data) {
  const activity = data.activity || {};
  const targets = activity.targets || [];
  const stats = activity.stats || {};
  const currentRun = activity.current_run || {};
  const history = activity.history || [];
  const body = document.getElementById('leadActivityBody');
  const state = document.getElementById('leadActivityState');
  const count = document.getElementById('leadActivityCount');
  const path = document.getElementById('leadActivityPath');
  if (!body || !state || !count || !path) return;
  count.innerText = `${targets.length} ${targets.length === 1 ? 'profile' : 'profiles'}`;
  state.className = `obf-status-pill ${targets.length ? (activity.status === 'completed' ? 'success' : 'active') : 'neutral'}`;
  state.innerText = targets.length ? titleCaseStatusClient(activity.status || 'prepared') : 'No session today';
  path.innerText = activity.file || 'No activity session created today';
  const setActivityStat = (valueId, detailId, value, detail) => {
    const valueNode = document.getElementById(valueId);
    const detailNode = document.getElementById(detailId);
    if (valueNode) valueNode.innerText = value;
    if (detailNode) detailNode.innerText = detail;
  };
  // The session can contain results from an earlier attempt. Display the
  // active watcher run separately, with the daily session retained in detail.
  const prepared = Number(currentRun.profiles_total ?? stats.profiles_prepared ?? targets.length ?? 0);
  const recorded = Number(currentRun.profiles_completed ?? stats.profiles_recorded ?? 0);
  const pending = Number(currentRun.profiles_pending ?? Math.max(0, prepared - recorded));
  const leads = Number(stats.unique_leads || 0);
  const activeSignals = Number(currentRun.active_signals ?? stats.active_signals ?? 0);
  const bridged = Number(stats.bridged_rows || 0);
  const failures = Number(stats.failures || 0);
  const hasCurrentRun = Boolean(currentRun.started_at);
  setActivityStat('activityStatPrepared', 'activityStatPreparedDetail', prepared, hasCurrentRun ? `This run · ${currentRun.prior_records || 0} earlier records excluded` : (activity.prepared_at ? `Prepared ${formatObfTime(activity.prepared_at, true)}` : 'No local session'));
  setActivityStat('activityStatRecorded', 'activityStatRecordedDetail', recorded, prepared ? `${Math.round((recorded / prepared) * 100)}% completed this run` : 'Waiting to run');
  setActivityStat('activityStatPending', 'activityStatPendingDetail', pending, hasCurrentRun ? 'Remaining in this run' : (failures ? `${failures} run ${failures === 1 ? 'failure' : 'failures'} recorded` : 'No failures recorded'));
  setActivityStat('activityStatLeads', 'activityStatLeadsDetail', leads, 'Across prepared profiles');
  setActivityStat('activityStatSignals', 'activityStatSignalsDetail', activeSignals, hasCurrentRun ? 'Signals found this run' : 'Active or very active');
  setActivityStat('activityStatBridged', 'activityStatBridgedDetail', bridged, 'Final rows this session');
  const historyBody = document.getElementById('activityHistoryBody');
  const historyCount = document.getElementById('activityHistoryCount');
  const quarantineIssues = Array.isArray(activity.quarantine_issues) ? activity.quarantine_issues : [];
  const quarantineBlock = document.getElementById('activityQuarantineBlock');
  const quarantineToggle = document.getElementById('activityQuarantineToggle');
  const quarantineContent = document.getElementById('activityQuarantineContent');
  const quarantineBody = document.getElementById('activityQuarantineBody');
  const quarantineCount = document.getElementById('activityQuarantineCount');
  if (quarantineCount) quarantineCount.innerText = `${quarantineIssues.length} ${quarantineIssues.length === 1 ? 'issue' : 'issues'}`;
  if (quarantineBlock) quarantineBlock.classList.toggle('has-issues', quarantineIssues.length > 0);
  if (quarantineToggle && quarantineContent && quarantineIssues.length > 0 && quarantineToggle.getAttribute('data-auto-opened') !== 'true') {
    quarantineToggle.setAttribute('aria-expanded', 'true');
    quarantineToggle.setAttribute('data-auto-opened', 'true');
    quarantineContent.hidden = false;
  }
  if (quarantineBody) {
    quarantineBody.innerHTML = quarantineIssues.length ? quarantineIssues.map(issue => {
      const profile = issue.profile_url
        ? `<a href="${escapeHtmlSafe(issue.profile_url)}" target="_blank" rel="noreferrer">Open profile</a>`
        : '—';
      return `<tr>
        <td><strong>${escapeHtmlSafe(issue.company || '—')}</strong></td>
        <td>${escapeHtmlSafe(issue.lead_id || '—')}</td>
        <td>${escapeHtmlSafe(issue.person || '—')}</td>
        <td><strong>${escapeHtmlSafe(issue.name || '—')}</strong><small>${escapeHtmlSafe(issue.title || '')}</small></td>
        <td><span class="obf-status-pill warning">${escapeHtmlSafe(String(issue.reason || 'profile issue').replaceAll('_', ' '))}</span></td>
        <td>${issue.recorded_at ? escapeHtmlSafe(formatObfTime(issue.recorded_at, true)) : '—'}</td>
        <td>${profile}</td>
      </tr>`;
    }).join('') : '<tr><td colspan="7"><div class="empty-state">No quarantined profile issues.</div></td></tr>';
  }
  if (historyCount) historyCount.innerText = `${history.length} ${history.length === 1 ? 'session' : 'sessions'}`;
  if (historyBody) {
    historyBody.innerHTML = history.length ? history.map(row => {
      const status = String(row.status || 'not_prepared');
      const tone = status === 'completed' ? 'success' : (status === 'blocked' || status.includes('fail') ? 'warning' : 'neutral');
      return `<tr>
        <td><strong>${escapeHtmlSafe(row.date || '—')}</strong></td>
        <td><span class="obf-status-pill ${tone}">${escapeHtmlSafe(titleCaseStatusClient(status))}</span></td>
        <td>${Number(row.profiles_prepared || 0)}</td>
        <td>${Number(row.profiles_recorded || 0)}</td>
        <td>${Number(row.profiles_pending || 0)}</td>
        <td>${Number(row.unique_leads || 0)}</td>
        <td>${Number(row.active_signals || 0)}</td>
        <td>${Number(row.bridged_rows || 0)}</td>
        <td>${Number(row.failures || 0)}</td>
        <td>${row.updated_at ? escapeHtmlSafe(formatObfTime(row.updated_at, true)) : '—'}</td>
      </tr>`;
    }).join('') : '<tr><td colspan="10"><div class="empty-state">No Activity Check sessions recorded.</div></td></tr>';
  }
  if (!targets.length) {
    body.innerHTML = '<tr><td colspan="6"><div class="empty-state">Prepare Activity Check to create its local queue.</div></td></tr>';
    return;
  }
  body.innerHTML = targets.map(target => {
    const profile = target.profile_url
      ? `<a href="${escapeHtmlSafe(target.profile_url)}" target="_blank" rel="noreferrer">Open profile</a>`
      : '—';
    return `<tr>
      <td><strong>${escapeHtmlSafe(target.company || '—')}</strong></td>
      <td>${escapeHtmlSafe(target.lead_id || '—')}</td>
      <td>${escapeHtmlSafe(target.prefix || '—')}</td>
      <td>${profile}</td>
      <td>${escapeHtmlSafe(target.activity_value || 'Pending')}</td>
      <td><span class="obf-status-pill ${target.status === 'recorded' ? 'success' : 'neutral'}">${escapeHtmlSafe(titleCaseStatusClient(target.status || 'prepared'))}</span></td>
    </tr>`;
  }).join('');
}

function renderLeadPrepDashboard(data) {
  currentLeadPrepDashboard = data;
  const config = data.config || {};
  if (todayDate) {
    todayDate.innerText = config.dashboard_date_override
      ? formatHeaderDate(new Date(`${config.dashboard_date_override}T12:00:00`), 'Test')
      : formatHeaderDate();
  }
  const run = data.run || {};
  const archive = data.archive || {};
  const hasToday = Boolean(run.is_today);
  const target = Number(hasToday ? run.fresh_target : config.base_volume || 60);
  const fresh = Number(hasToday ? run.fresh_count : 0);
  const progress = target ? Math.min(100, Math.round((fresh / target) * 100)) : 0;
  const topUpsEnabled = config.fresh_volume_top_up_mode !== 'off';
  const review = data.review || {};
  initializeLeadReviewDraft(review);
  const historical = data.historical || {};
  const reviewCopy = leadReviewStatusCopy(review, Boolean(config.approval_gate_enabled));

  document.getElementById('leadReviewStatusTitle').innerText = reviewCopy.title;
  document.getElementById('leadReviewStatusDetail').innerText = reviewCopy.detail;
  document.getElementById('leadReviewPreparedCount').innerText = Number(review.prepared_count || 0);
  document.getElementById('leadReviewCompleteState').innerText = review.review_complete ? 'Complete' : 'Not complete';
  updateLeadDraftSummary();
  const activeDeadline = review.window === 'first' ? review.first_deadline_at : review.fallback_deadline_at;
  document.getElementById('leadReviewDeadlineLabel').innerText = review.window === 'first'
    ? 'First processing window'
    : review.window === 'fallback' ? 'Fallback processing window' : 'Fallback window reached';
  document.getElementById('leadReviewCountdown').innerText = formatLeadCountdown(activeDeadline);
  document.getElementById('leadReviewDeadlineTime').innerText = formatLeadDeadlineTime(activeDeadline);

  document.getElementById('leadHistoricalTotal').innerText = Number(historical.total_leads || 0);
  document.getElementById('leadHistoricalCaseStudy').innerText = Number(historical.total_case_study_worthy || 0);
  const caseStudyRate = historical.total_leads
    ? Math.round((Number(historical.total_case_study_worthy || 0) / Number(historical.total_leads)) * 100)
    : 0;
  document.getElementById('leadHistoricalCaseStudyRate').innerText = `${caseStudyRate}% of Lead Review rows`;
  document.getElementById('leadHistoricalDateGroups').innerText = `${Number(historical.date_groups || 0)} days`;
  const cacheBadge = document.getElementById('leadReviewCacheBadge');
  cacheBadge.className = `obf-status-pill ${review.cache?.cached_at ? 'success' : 'neutral'}`;
  cacheBadge.innerText = review.cache?.cached_at ? 'Cached' : 'Not cached';
  document.getElementById('leadReviewCacheTime').innerText = review.cache?.cached_at
    ? `Updated ${formatObfTime(review.cache.cached_at, true)} · automatic refresh is 10 minutes after prep`
    : 'Refreshes 10 minutes after preparation';

  document.getElementById('leadPrepSequenceTitle').innerText = data.is_running
    ? (topUpsEnabled ? 'Preparing and scanning today’s batch' : 'Preparing one review batch')
    : hasToday
      ? (run.target_met
          ? 'Fresh daily volume restored'
          : run.settled_for_day
            ? 'Base batch prepared — settled for today'
            : 'Preparation needs attention')
      : 'Waiting for today’s preparation';
  document.getElementById('leadPrepSequenceDetail').innerText = data.is_running
    ? (topUpsEnabled
        ? 'The detection-only scan is tagging overlaps and adding replacement waves; it is not reusing research.'
        : 'The detection-only scan is tagging overlaps; top-ups are disabled, so preparation will stop after the base batch.')
    : hasToday
      ? `${run.selected_count || 0} total review rows · ${run.archive_match_count || 0} archive matches · ${run.top_up_count || 0} top-up rows${run.top_up_suppressed_count ? ` · ${run.top_up_suppressed_count} replacements intentionally skipped` : ''}`
      : (topUpsEnabled
          ? 'One base batch will be scanned; overlap-driven top-ups will restore fresh volume.'
          : 'One base batch will be scanned and accepted as the full preparation for the day.');
  document.getElementById('leadPrepProgressCount').innerText = `${fresh} / ${target}`;
  document.getElementById('leadPrepProgressFill').style.width = `${progress}%`;
  document.getElementById('leadPrepLiveDot').classList.toggle('running', Boolean(data.is_running));

  const toggle = document.getElementById('leadPrepAutonomyToggle');
  toggle.setAttribute('aria-checked', String(Boolean(config.autonomous_prep_enabled)));
  document.getElementById('leadPrepAutonomyCopy').innerText = config.autonomous_prep_enabled
    ? `Enabled · prepares at ${config.prep_time || '14:00'}`
    : 'Disabled · manual preparation only';
  const editing = Boolean(document.activeElement?.closest?.('.lead-settings-grid'));
  if (!editing) {
    document.getElementById('leadPrepTime').value = config.prep_time || '14:00';
    document.getElementById('leadPrepVolume').value = config.base_volume || 60;
    document.getElementById('leadPrepApprovalGate').value = config.approval_gate_enabled ? 'enabled' : 'disabled';
    document.getElementById('leadPrepOverlapScanMode').value = config.overlap_scan_mode === 'off' ? 'off' : 'auto';
    document.getElementById('leadPrepTopUpMode').value = topUpsEnabled ? 'auto' : 'off';
  }

  const watcherBadge = document.getElementById('leadPrepWatcherBadge');
  const watcherTone = data.watcher?.status === 'completed'
    ? 'success'
    : data.watcher?.status === 'failed_terminal'
      ? 'danger'
      : config.autonomous_prep_enabled ? 'active' : 'neutral';
  watcherBadge.className = `obf-status-pill ${watcherTone}`;
  watcherBadge.innerText = titleCaseStatusClient(data.watcher?.status || 'disabled');
  document.getElementById('leadPrepControlNote').innerText = topUpsEnabled
    ? 'Fresh-volume top-ups are enabled. Replacement waves stop when fresh volume is restored or the source is exhausted.'
    : 'Fresh-volume top-ups are disabled. The overlap scan still runs, but preparation stops after one base batch and settles for the day.';

  document.getElementById('leadArchiveAvailable').innerText = archive.available || 0;
  document.getElementById('leadArchiveTotal').innerText = `${archive.total || 0} total entries · ${archive.consumed || 0} consumed`;
  document.getElementById('leadArchiveMatches').innerText = hasToday ? (run.archive_match_count || 0) : 0;
  document.getElementById('leadFreshCount').innerText = `${fresh} / ${target}`;
  document.getElementById('leadFreshState').innerText = hasToday
    ? (run.target_met
        ? 'target restored'
        : run.settled_for_day
          ? 'single batch accepted'
          : run.source_exhausted ? 'source exhausted' : 'target not yet restored')
    : 'not prepared today';
  document.getElementById('leadTopupCount').innerText = hasToday ? (run.top_up_count || 0) : 0;
  document.getElementById('leadConflictCount').innerText = hasToday ? (run.conflict_count || 0) : 0;

  const runBadge = document.getElementById('leadPrepRunBadge');
  runBadge.className = `obf-status-pill ${hasToday ? (run.settled_for_day ? 'success' : 'warning') : 'neutral'}`;
  runBadge.innerText = hasToday ? titleCaseStatusClient(run.status || 'prepared') : 'No run today';
  document.getElementById('leadPrepRunPath').innerText = hasToday
    ? run.file
    : review.cache?.path || archive.path || 'No local state for today';
  document.getElementById('leadPrepLastUpdated').innerText = data.last_updated ? `Updated ${formatObfTime(data.last_updated, true)}` : '—';
  document.getElementById('leadPrepRunBtn').disabled = Boolean(data.is_running || hasToday);
  const processingAutomationState = document.getElementById('leadProcessingAutomationState');
  if (processingAutomationState) {
    const paused = Boolean(review.processing_automation?.paused);
    processingAutomationState.className = `obf-status-pill ${paused ? 'warning' : 'neutral'}`;
    processingAutomationState.innerText = paused ? 'Paused for testing' : 'Outside dashboard';
  }
  const editingPreparedTable = Boolean(document.activeElement?.closest?.('.lead-prepared-table'));
  if (!editingPreparedTable) renderPreparedLeads(data);
  else renderLeadPrepWaves(data);
  renderLeadFinalReport(data);
  const editingProcessingTable = Boolean(document.activeElement?.closest?.('.lead-processing-table'));
  if (!editingProcessingTable) renderLeadProcessing(data);
  renderLeadActivity(data);
  if (currentOverviewStats) renderOverviewWorkflow(currentOverviewStats);
}

async function refreshLeadPrepDashboard() {
  const data = await ipcRenderer.invoke('read-lead-prep-dashboard');
  if (data) renderLeadPrepDashboard(data);
}

function readLeadPrepSettings(enabledOverride = null) {
  const toggle = document.getElementById('leadPrepAutonomyToggle');
  return {
    autonomous_prep_enabled: enabledOverride === null
      ? toggle.getAttribute('aria-checked') === 'true'
      : enabledOverride,
    prep_time: document.getElementById('leadPrepTime').value,
    base_volume: Number(document.getElementById('leadPrepVolume').value),
    approval_gate_enabled: document.getElementById('leadPrepApprovalGate').value === 'enabled',
    overlap_scan_mode: document.getElementById('leadPrepOverlapScanMode').value,
    fresh_volume_top_up_mode: document.getElementById('leadPrepTopUpMode').value
  };
}

async function saveLeadPrepSettings(enabledOverride = null) {
  const settings = readLeadPrepSettings(enabledOverride);
  if (!settings.base_volume || settings.base_volume < 1 || settings.base_volume > 500) {
    appendToConsole('Lead Prep base volume must be between 1 and 500.', 'error');
    return false;
  }
  const result = await ipcRenderer.invoke('save-lead-prep-settings', settings);
  if (!result?.ok) {
    appendToConsole(result?.error || 'Could not save Lead Prep settings.', 'error');
    await refreshLeadPrepDashboard();
    return false;
  }
  appendToConsole('Lead Prep settings saved.', 'success');
  await refreshLeadPrepDashboard();
  return true;
}

document.getElementById('leadPrepSaveBtn')?.addEventListener('click', () => saveLeadPrepSettings());
document.getElementById('leadPrepAutonomyToggle')?.addEventListener('click', async event => {
  const enabled = event.currentTarget.getAttribute('aria-checked') !== 'true';
  event.currentTarget.setAttribute('aria-checked', String(enabled));
  await saveLeadPrepSettings(enabled);
});

document.getElementById('leadReviewRefreshBtn')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.innerText = 'Refreshing…';
  const result = await ipcRenderer.invoke('refresh-lead-review-cache');
  if (!result?.ok) {
    appendToConsole(result?.error || 'Lead Review refresh failed.', 'error');
  } else {
    appendToConsole('Lead Review cache refreshed from the Sheet.', 'success');
    renderLeadPrepDashboard(result.dashboard);
  }
  button.disabled = false;
  button.innerText = 'Refresh now';
});

document.getElementById('leadPreparedFilter')?.addEventListener('change', () => {
  if (currentLeadPrepDashboard) renderPreparedLeads(currentLeadPrepDashboard);
});
document.getElementById('leadPreparedSearch')?.addEventListener('input', () => {
  if (currentLeadPrepDashboard) renderPreparedLeads(currentLeadPrepDashboard);
});

document.getElementById('activityHistoryToggle')?.addEventListener('click', event => {
  const button = event.currentTarget;
  const content = document.getElementById('activityHistoryContent');
  if (!content) return;
  const expanded = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(expanded));
  content.hidden = !expanded;
});

document.getElementById('activityQuarantineToggle')?.addEventListener('click', event => {
  const button = event.currentTarget;
  const content = document.getElementById('activityQuarantineContent');
  if (!content) return;
  const expanded = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(expanded));
  content.hidden = !expanded;
});

document.getElementById('leadPreparedBody')?.addEventListener('change', async event => {
  const row = event.target.closest('tr[data-lead-id]');
  if (!row) return;
  const decision = leadReviewDraft.get(String(row.dataset.leadId || ''));
  if (!decision) return;
  if (event.target.classList.contains('lead-approval-check')) {
    decision.approved = event.target.checked;
    decision.dirty = decision.approved !== decision.expected_approved || decision.use !== decision.expected_use;
    updateLeadDraftSummary();
    return;
  }
  if (event.target.classList.contains('lead-use-select')) {
    decision.use = event.target.value;
    decision.dirty = decision.approved !== decision.expected_approved || decision.use !== decision.expected_use;
    updateLeadDraftSummary();
  }
});

document.getElementById('leadReviewHandoffBtn')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  const review = currentLeadPrepDashboard?.review || {};
  const rows = [...leadReviewDraft.values()].map(item => ({
    row: item.row,
    run_id: item.run_id,
    expected_approved: item.expected_approved,
    expected_use: item.expected_use,
    approved: item.approved,
    use: item.use
  }));
  if (!review.group_row || !rows.length) return;
  button.disabled = true;
  const buttonSmall = button.querySelector('small');
  const buttonStrong = button.querySelector('strong');
  if (buttonSmall) buttonSmall.innerText = 'Saving review';
  if (buttonStrong) buttonStrong.innerText = 'Syncing…';
  const result = await ipcRenderer.invoke('sync-lead-review-group', {
    group_row: review.group_row,
    date: review.date,
    rows
  });
  if (!result?.ok) {
    appendToConsole(result?.error || 'Review handoff failed. Nothing was moved.', 'error');
    if (buttonSmall) buttonSmall.innerText = 'Save review';
    if (buttonStrong) buttonStrong.innerText = 'Sync & continue';
    updateLeadDraftSummary();
    return;
  }
  leadReviewDraftKey = '';
  renderLeadPrepDashboard(result.dashboard);
  showLeadTab('processing', true);
  appendToConsole('Review decisions synced, Design Review Complete checked, and Lead Processing opened.', 'success');
  if (buttonSmall) buttonSmall.innerText = 'Save review';
  if (buttonStrong) buttonStrong.innerText = 'Sync & continue';
});

function manualResearchExecutives(row) {
  const executives = [0, 1, 2].map(personIndex => {
    const value = field => row.querySelector(
      `.manual-research-input[data-person-index="${personIndex}"][data-person-field="${field}"]`
    )?.value.trim() || '';
    return {
      name: value('name'),
      title: value('title'),
      linkedin_url: value('linkedin_url'),
      email: value('email')
    };
  });
  return executives;
}

function setManualResearchSaveStatus(row, status, message) {
  const indicator = row?.querySelector('.lead-research-autosave');
  if (!indicator) return;
  indicator.className = `lead-research-autosave ${status || ''}`.trim();
  indicator.innerHTML = `<i></i>${escapeHtmlSafe(message)}`;
}

function queueManualResearchSave(row, delay = 800) {
  const leadId = String(row?.dataset.processingLeadId || '');
  if (!leadId) return;
  const saveState = leadResearchAutosave.get(leadId) || { timer: null, dirty: false, saving: false };
  saveState.dirty = true;
  if (saveState.timer) clearTimeout(saveState.timer);
  saveState.timer = window.setTimeout(() => {
    saveState.timer = null;
    persistManualResearch(row, leadId);
  }, delay);
  leadResearchAutosave.set(leadId, saveState);
  setManualResearchSaveStatus(row, 'pending', delay ? 'Saving shortly' : 'Saving…');
}

async function persistManualResearch(row, leadId) {
  const saveState = leadResearchAutosave.get(leadId);
  if (!saveState || saveState.saving) return;
  saveState.saving = true;
  saveState.dirty = false;
  const executives = manualResearchExecutives(row);
  const invalidLinkedin = [...row.querySelectorAll('.manual-research-input[data-person-field="linkedin_url"]')]
    .find(input => input.value.trim() && !/^https?:\/\/([a-z]{2,3}\.)?linkedin\.com\/in\/[^?#\s]+(?:[?#].*)?$/i.test(input.value.trim()));
  if (invalidLinkedin) {
    invalidLinkedin.classList.add('invalid');
    setManualResearchSaveStatus(row, 'error', 'Finish LinkedIn URL');
    saveState.saving = false;
    return;
  }
  setManualResearchSaveStatus(row, 'saving', 'Saving…');
  const result = await ipcRenderer.invoke('save-manual-lead-research', {
    lead_id: leadId,
    executives
  });
  if (!result?.ok) {
    setManualResearchSaveStatus(row, 'error', 'Save failed');
    saveState.saving = false;
    appendToConsole(result?.error || 'Manual research could not be saved.', 'error');
    return;
  }
  currentLeadPrepDashboard = result.dashboard;
  const savedLead = (result.dashboard.processing?.leads || [])
    .find(lead => String(lead.run_id || '') === leadId);
  setManualResearchSaveStatus(row, savedLead?.processing_status === 'manual_ready' ? 'saved' : 'saved', 'Saved locally');
  saveState.saving = false;
  if (saveState.dirty) {
    persistManualResearch(row, leadId);
    return;
  }
  if (!row.contains(document.activeElement)) renderLeadPrepDashboard(result.dashboard);
}

document.getElementById('leadProcessingBody')?.addEventListener('input', event => {
  if (!event.target.classList.contains('manual-research-input')) return;
  event.target.classList.remove('invalid');
  const row = event.target.closest('tr[data-processing-lead-id]');
  if (row) queueManualResearchSave(row);
});

document.getElementById('leadProcessingBody')?.addEventListener('focusout', event => {
  if (!event.target.classList.contains('manual-research-input')) return;
  const row = event.target.closest('tr[data-processing-lead-id]');
  if (row) queueManualResearchSave(row, 0);
});

document.getElementById('leadManualBridgeBtn')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  if (button.disabled) return;
  const approved = window.confirm(
    'Bridge this completed research group to Pre-final? This writes the saved local research to the Sheet.'
  );
  if (!approved) return;
  button.disabled = true;
  button.querySelector('span').innerText = 'Bridging…';
  const result = await ipcRenderer.invoke('bridge-manual-lead-processing', {});
  if (!result?.ok) {
    button.disabled = false;
    button.querySelector('span').innerText = 'Bridge completed research';
    appendToConsole(result?.error || 'Manual research bridge failed.', 'error');
    return;
  }
  currentLeadPrepDashboard = result.dashboard;
  renderLeadPrepDashboard(result.dashboard);
  appendToConsole('Completed manual research bridged to Pre-final.', 'success');
});

// Category Monitor Nodes
const actMonitorStatus = document.getElementById('actMonitorStatus');
const actMonitorTimer = document.getElementById('actMonitorTimer');
const actMonitorProgress = document.getElementById('actMonitorProgress');
const actChartFill = document.getElementById('actChartFill');

const fuMonitorStatus = document.getElementById('fuMonitorStatus');
const fuMonitorTimer = document.getElementById('fuMonitorTimer');
const fuMonitorProgress = document.getElementById('fuMonitorProgress');
const fuChartFill = document.getElementById('fuChartFill');

const wdMonitorStatus = document.getElementById('wdMonitorStatus');
const wdMonitorTimer = document.getElementById('wdMonitorTimer');
const wdMonitorProgress = document.getElementById('wdMonitorProgress');
const wdCooldownVal = document.getElementById('wdCooldownVal');
const wdChartFill = document.getElementById('wdChartFill');

const queueList = document.getElementById('queueList');

// Console Log elements
const consolePanel = document.getElementById('consolePanel');
const consoleLog = document.getElementById('consoleLog');
const consoleStatusIndicator = document.getElementById('consoleStatusIndicator');
const consoleStatusText = document.getElementById('consoleStatusText');
const autoScrollCheck = document.getElementById('autoScrollCheck');
const clearConsoleBtn = document.getElementById('clearConsoleBtn');
const stopProcessBtn = document.getElementById('stopProcessBtn');
const toggleConsoleBtn = document.getElementById('toggleConsoleBtn');

// Parameters Drawer elements
const paramDrawer = document.getElementById('paramDrawer');
const paramTitle = document.getElementById('paramTitle');
const paramLimit = document.getElementById('paramLimit');
const paramLimitLabel = document.getElementById('paramLimitLabel');
const closeParamBtn = document.getElementById('closeParamBtn');
const launchWithParamBtn = document.getElementById('launchWithParamBtn');

// State tracking variables
let currentRunningTaskElement = null; // Stored trigger button
let currentCategory = ''; // 'activity', 'followup', or 'withdrawal'
let scriptChain = [];
let isConsoleExpanded = false;

// Real-time synchronization is driven globally via refreshStats

// Play completion chime
function playSuccessChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc1 = ctx.createOscillator();
    const gainNode1 = ctx.createGain();
    osc1.type = 'sine';
    osc1.frequency.setValueAtTime(1568, ctx.currentTime); // G6 note
    gainNode1.gain.setValueAtTime(0.001, ctx.currentTime);
    gainNode1.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.05);
    gainNode1.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.85);

    const osc2 = ctx.createOscillator();
    const gainNode2 = ctx.createGain();
    osc2.type = 'sine';
    osc2.frequency.setValueAtTime(1318, ctx.currentTime); // E6 note
    gainNode2.gain.setValueAtTime(0.001, ctx.currentTime);
    gainNode2.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.05);
    gainNode2.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.65);

    osc1.connect(gainNode1);
    gainNode1.connect(ctx.destination);

    osc2.connect(gainNode2);
    gainNode2.connect(ctx.destination);

    osc1.start();
    osc2.start();

    osc1.stop(ctx.currentTime + 0.85);
    osc2.stop(ctx.currentTime + 0.65);
  } catch (err) {
    console.error('Audio chime error:', err);
  }
}

// Collapsible Category Sections Accordion triggers
categoryHeaders.forEach(header => {
  header.addEventListener('click', () => {
    header.classList.toggle('collapsed');
  });
});

// Category pills filter triggers
filterPills.forEach(pill => {
  pill.addEventListener('click', () => {
    filterPills.forEach(p => p.classList.remove('active'));
    pill.classList.add('active');

    const filterVal = pill.getAttribute('data-filter');

    categorySections.forEach(section => {
      const sectionId = section.id;
      if (filterVal === 'all') {
        section.style.display = sectionId === 'section-history' ? 'none' : 'flex';
      } else if (filterVal === 'history') {
        section.style.display = sectionId === 'section-history' ? 'flex' : 'none';
      } else if (filterVal === 'activity' && sectionId === 'section-activity') {
        section.style.display = 'flex';
      } else if (filterVal === 'followup' && sectionId === 'section-followup') {
        section.style.display = 'flex';
      } else if (filterVal === 'withdrawal' && sectionId === 'section-withdrawal') {
        section.style.display = 'flex';
      } else if (sectionId === 'section-queue' && filterVal !== 'history') {
        section.style.display = 'flex'; // Keep the active queue visible except in history
      } else {
        section.style.display = 'none';
      }
    });

    if (filterVal === 'history') {
      loadHistory();
    }
  });
});

// Close/Collapse App Button Trigger
if (closeAppBtn) {
  closeAppBtn.addEventListener('click', () => {
    ipcRenderer.send('toggle-drawer');
  });
}

if (fullScreenBtn && isElectronRuntime) {
  fullScreenBtn.addEventListener('click', () => ipcRenderer.send('toggle-fullscreen'));
}

// Helper to update a static task checkbox visual state
function updateCheckmarkUI(cardId, isChecked) {
  const card = document.getElementById(cardId);
  if (!card) return;

  const checkbox = card.querySelector('.task-checkbox');
  if (isChecked) {
    card.classList.add('done-item');
    checkbox.className = 'task-checkbox checked';
  } else {
    card.classList.remove('done-item');
    checkbox.className = 'task-checkbox';
  }
}

// Calculate and update the daily progress bar at the top (based on 6 checklist states)
function updateDailyProgress(stats) {
  const followupsEnabled = stats.followups_enabled !== false;
  const checkpoints = [
    stats.activity_prep_done,
    stats.activity_run_done,
    ...(followupsEnabled ? [stats.followup_prep_done, stats.followup_daytime_done] : []),
    stats.withdrawal_prep_done,
    stats.withdrawal_execute_done,
  ];
  const completedCount = checkpoints.filter(Boolean).length;
  const totalCount = checkpoints.length;
  const followupLegend = document.getElementById('overviewFollowupLegend');
  if (followupLegend) followupLegend.classList.toggle('disabled', !followupsEnabled);

  if (progressFraction) progressFraction.innerText = `${completedCount} / ${totalCount} completed`;
  const pct = totalCount ? Math.round((completedCount / totalCount) * 100) : 0;
  if (progressFill) progressFill.style.width = `${pct}%`;
  const tasksValue = document.getElementById('overviewTasksValue');
  const tasksDetail = document.getElementById('overviewTasksDetail');
  if (tasksValue) tasksValue.innerText = `${completedCount} / ${totalCount}`;
  if (tasksDetail) tasksDetail.innerText = completedCount === totalCount ? 'all active checkpoints complete' : `${totalCount - completedCount} checkpoint${totalCount - completedCount === 1 ? '' : 's'} remaining`;
}

let currentOverviewStats = null;
function renderOverviewWorkflow(stats) {
  const obf = currentObfDashboard || {};
  const obfSummary = obf.summary || {};
  const lead = currentLeadPrepDashboard || {};
  const review = lead.review || {};
  const activityPrepared = Number(stats.activity?.prepared || 0);
  const followupsEnabled = stats.followups_enabled !== false;
  const followupPrepared = followupsEnabled ? Number(stats.followups?.prepared || 0) : 0;
  const withdrawalPrepared = Number(stats.withdrawals?.prepared || 0);
  const set = (stateId, detailId, state, detail) => {
    const stateEl = document.getElementById(stateId);
    const detailEl = document.getElementById(detailId);
    if (stateEl) stateEl.innerText = state;
    if (detailEl) detailEl.innerText = detail;
  };
  const obfDone = Number(obfSummary.resolved || 0);
  const obfPlanned = Number(obfSummary.target || obfSummary.planned || 0);
  set('overviewObfState', obfPlanned ? `${obfDone} / ${obfPlanned} resolved` : 'Ready to prepare', obfPlanned ? `${obf.phase || 'prepared'} · ${obfSummary.sent || 0} sent` : 'No prepared OBF run for today');
  const prepared = Number(review.prepared_count || lead.run?.selected_count || 0);
  const approved = Number(review.approved_count || 0);
  set('overviewLeadPrepState', prepared ? `${approved} / ${prepared} approved` : 'Waiting for review', prepared ? `${review.status || 'review open'} · ${review.window || 'today'} window` : 'No lead-review group is ready yet');
  const outreachPrepared = followupPrepared + withdrawalPrepared + activityPrepared;
  const outreachDone = Number(stats.followups?.completed || 0) + Number(stats.withdrawals?.completed || 0) + Number(stats.activity?.completed || 0);
  set('overviewOutreachState', outreachPrepared ? `${outreachDone} / ${outreachPrepared} actions complete` : 'Queues clear', outreachPrepared ? `${followupPrepared} follow-up · ${withdrawalPrepared} withdrawal · ${activityPrepared} activity` : 'No active follow-up, withdrawal, or activity queue');
  const next = document.getElementById('overviewNextStep');
  if (next) {
    next.innerText = !obfPlanned ? 'Next: prepare OBF' : prepared && approved < prepared ? 'Next: finish Lead Prep review' : outreachPrepared ? 'Next: clear Outreach work' : 'Operating flow is clear';
  }

  const priorities = [];
  const watcherAlerts = Array.isArray(stats.watcher?.alerts) ? stats.watcher.alerts : [];
  if (watcherAlerts.length) {
    const alert = watcherAlerts[0];
    const detail = String(alert.detail || alert.reason || 'A watcher checkpoint needs attention.').replaceAll('_', ' ');
    priorities.push({ tone: 'danger', title: `${titleCaseStatusClient(alert.workflow || 'Watcher')} needs attention`, detail, page: alert.workflow === 'obf' ? 'obf' : (alert.workflow === 'lead_prep' ? 'lead-prep' : 'outreach') });
  }
  const obfIssues = obf.issues || [];
  if (obfIssues.length) priorities.push({ tone: 'danger', title: `${obfIssues.length} OBF exception${obfIssues.length === 1 ? '' : 's'} need review`, detail: obfIssues[0].detail || obfIssues[0].title || 'Open OBF to resolve the exception.', page: 'obf' });
  if (prepared && approved < prepared) priorities.push({ tone: 'warning', title: `${prepared - approved} lead${prepared - approved === 1 ? '' : 's'} awaiting approval`, detail: `${approved} of ${prepared} prepared leads are approved.`, page: 'lead-prep' });
  if (withdrawalPrepared || followupPrepared || activityPrepared) priorities.push({ tone: 'warning', title: `${outreachPrepared - outreachDone} outreach action${outreachPrepared - outreachDone === 1 ? '' : 's'} remain`, detail: `${followupPrepared} follow-up, ${withdrawalPrepared} withdrawal, ${activityPrepared} activity checks prepared.`, page: 'outreach' });
  if (!priorities.length) priorities.push({ tone: 'good', title: 'No operating blockers', detail: 'The three core workstreams are clear or waiting for their next scheduled window.', page: 'tasks' });
  const priorityList = document.getElementById('overviewPriorityList');
  if (priorityList) priorityList.innerHTML = priorities.slice(0, 3).map(item => `<div class="overview-priority ${item.tone}"><i></i><div><strong>${escapeHtmlSafe(item.title)}</strong><span>${escapeHtmlSafe(item.detail)}</span></div><button data-overview-page="${item.page}">Review</button></div>`).join('');
  priorityList?.querySelectorAll('[data-overview-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.overviewPage)));

  const lanes = [
    { page: 'obf', name: 'OBF', subtitle: 'Daily outbound execution', metric: obfPlanned ? `${obfDone} / ${obfPlanned}` : '—', metricLabel: 'resolved', status: obf.is_running ? 'Running now' : (obfPlanned ? (obf.phase || 'Prepared') : 'Not prepared'), tone: obfIssues.length ? 'danger' : (obf.is_running ? 'warning' : ''), facts: [['Sent', obfSummary.sent || 0], ['Exceptions', obfIssues.length], ['Retries', obfSummary.retries || 0], ['Last event', obfSummary.last_event_time ? formatObfTime(obfSummary.last_event_time, true) : '—']] },
    { page: 'lead-prep', name: 'Lead Prep', subtitle: 'Review and approvals', metric: prepared ? `${approved} / ${prepared}` : '—', metricLabel: 'approved', status: review.status || (prepared ? 'Review open' : 'Waiting'), tone: prepared && approved < prepared ? 'warning' : '', facts: [['Fresh', review.fresh_count || lead.run?.fresh_count || 0], ['Approved', approved], ['Conflicts', review.conflict_count || 0], ['Deadline', review.next_deadline_at ? formatObfTime(review.next_deadline_at, true) : '—']] },
    { page: 'outreach', name: 'Outreach', subtitle: 'Follow-ups, withdrawals, activity', metric: outreachPrepared ? `${outreachDone} / ${outreachPrepared}` : '—', metricLabel: 'actions complete', status: outreachPrepared ? 'Queue active' : 'Queues clear', tone: outreachPrepared ? 'warning' : '', facts: [['Follow-ups', `${stats.followups?.completed || 0}/${followupPrepared}`], ['Withdrawals', `${stats.withdrawals?.completed || 0}/${withdrawalPrepared}`], ['Activity', `${stats.activity?.completed || 0}/${activityPrepared}`], ['Running', stats.followups?.is_running || stats.withdrawals?.is_running || stats.activity?.is_running ? 'Yes' : 'No']] }
  ];
  const laneRoot = document.getElementById('overviewLanes');
  if (laneRoot) laneRoot.innerHTML = lanes.map(lane => `<article class="overview-lane"><div class="overview-lane-top"><div><h3>${lane.name}</h3><p>${lane.subtitle}</p></div><span class="overview-lane-status ${lane.tone}">${escapeHtmlSafe(lane.status)}</span></div><div class="overview-lane-metric">${lane.metric}<small>${lane.metricLabel}</small></div><div class="overview-lane-facts">${lane.facts.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtmlSafe(String(value))}</strong></div>`).join('')}</div><button class="secondary-btn overview-open-btn" data-overview-page="${lane.page}">Open ${lane.name}</button></article>`).join('');
  laneRoot?.querySelectorAll('[data-overview-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.overviewPage)));

  const scheduleRoot = document.getElementById('overviewSchedule');
  if (scheduleRoot) {
    const obfConfig = obf.config || {};
    const leadConfig = lead.config || {};
    const obfPrepared = Boolean(obf.prepared_exists);
    const obfFinished = Boolean(obfSummary.planned && obfSummary.resolved >= obfSummary.planned);
    const leadPrepared = Boolean(lead.run?.is_today || lead.run?.created_at?.startsWith(stats.today));
    const windows = [
      { time: obfConfig.prep_time || '08:25', name: 'OBF prep', detail: 'Freeze outbound workload', state: obf.active_action === 'prepare' ? 'active' : (obfPrepared ? 'completed' : 'pending') },
      { time: obfConfig.exec_time || '08:30', name: 'OBF run', detail: 'Execute approved outreach', state: obf.is_running && obf.active_action === 'execute' ? 'active' : (obfFinished ? 'completed' : 'pending') },
      { time: leadConfig.prep_time || '14:00', name: 'Lead Prep', detail: 'Prepare the review group', state: lead.is_running ? 'active' : (leadPrepared ? 'completed' : 'pending') },
      ...(followupsEnabled ? [{ time: '11:40', name: 'Follow-ups', detail: 'Repeat every two hours', state: stats.followups?.is_running ? 'active' : (stats.followup_daytime_done ? 'completed' : 'pending') }] : []),
      { time: '18:00', name: 'Withdrawals', detail: 'Clear stale invitations', state: stats.withdrawals?.is_running ? 'active' : (stats.withdrawal_execute_done ? 'completed' : 'pending') },
      { time: '21:00', name: 'Activity check', detail: 'Review and bridge activity', state: stats.activity?.is_running ? 'active' : (stats.activity_run_done ? 'completed' : 'pending') }
    ].sort((a, b) => a.time.localeCompare(b.time));
    const nowMinutes = new Date().getHours() * 60 + new Date().getMinutes();
    const toMinutes = time => { const [h, m] = time.split(':').map(Number); return h * 60 + m; };
    const activeWindow = windows.find(item => item.state === 'active');
    const nextWindow = activeWindow ? null : windows.find(item => item.state === 'pending' && toMinutes(item.time) >= nowMinutes) || windows.find(item => item.state === 'pending');
    const headline = activeWindow ? `active: ${activeWindow.name}` : nextWindow ? `next: ${nextWindow.name} ${nextWindow.time}` : 'all scheduled runs complete';
    const stateLabel = { completed: 'Completed', active: 'Active now', pending: 'Pending' };
    scheduleRoot.innerHTML = `<span class="section-kicker">Today’s run order · ${escapeHtmlSafe(headline)}</span><div class="overview-schedule-track">${windows.map(item => `<div class="overview-schedule-item ${item.state} ${item === nextWindow ? 'current' : ''}"><time>${item.time}</time><strong>${item.name}</strong><span>${item.detail}</span><em>${stateLabel[item.state]}</em></div>`).join('')}</div>`;
  }
}

let expandedQueueItems = new Set();

function renderWithdrawalState(withdrawals) {
  const body = document.getElementById('withdrawalStateBody');
  const count = document.getElementById('withdrawalStateCount');
  const path = document.getElementById('withdrawalStatePath');
  if (!body) return;
  const rows = withdrawals?.list || [];
  if (count) count.innerText = `${rows.length} rows`;
  if (path) path.innerText = withdrawals?.session_path || 'state/withdrawal_sessions/<today>.json';
  if (!withdrawals?.exists) {
    body.innerHTML = '<tr><td colspan="9"><div class="empty-state">No local withdrawal queue exists for today. Prepare batches to build it.</div></td></tr>';
    return;
  }
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="9"><div class="empty-state">Today’s withdrawal queue is prepared and empty.</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(row => `
    <tr>
      <td>${row.index}</td>
      <td><strong>${escapeHtmlSafe(row.company)}</strong><small>ID ${escapeHtmlSafe(row.id)}</small></td>
      <td>${escapeHtmlSafe(row.contact_name)}</td>
      <td>${escapeHtmlSafe(row.sent_at || '—')}</td>
      <td>${row.days_left ?? '—'}</td>
      <td>${row.batch ?? '—'}</td>
      <td>${escapeHtmlSafe(row.navigation || '—')}</td>
      <td><span class="obf-status-pill ${row.status === 'Withdrawn' ? 'success' : (row.status === 'Prepared' ? 'neutral' : 'warning')}">${escapeHtmlSafe(row.status)}</span></td>
      <td>${formatObfTime(row.last_update, true)}</td>
    </tr>
  `).join('');
}

// Fetch and load today's log statistics
async function refreshStats() {
  const stats = await ipcRenderer.invoke('read-stats');
  currentOverviewStats = stats;
  const followupsEnabled = stats.followups_enabled !== false;

  // 1. Update checklist checkmarks
  updateCheckmarkUI('check-act-prep', stats.activity_prep_done);
  updateCheckmarkUI('check-act-run', stats.activity_run_done);

  updateCheckmarkUI('check-fu-prep', stats.followup_prep_done);
  updateCheckmarkUI('check-fu-run', stats.followup_daytime_done);

  updateCheckmarkUI('check-wd-prep', stats.withdrawal_prep_done);
  updateCheckmarkUI('check-wd-run', stats.withdrawal_execute_done);

  // 2. Update daily progress bar
  updateDailyProgress(stats);

  const obfSummary = currentObfDashboard?.summary || {};
  const outreachSuccesses = Number(obfSummary.sent || 0) + Number(obfSummary.reconciled || 0);
  const outreachFailures = Number(obfSummary.requires_email || 0) + Number(obfSummary.failed || 0) + Number(obfSummary.timed_out || 0);
  const outreachResolved = outreachSuccesses + outreachFailures;
  const outreachSuccessValue = document.getElementById('overviewOutreachSuccessValue');
  const outreachSuccessDetail = document.getElementById('overviewOutreachSuccessDetail');
  if (outreachSuccessValue) outreachSuccessValue.innerText = outreachResolved ? `${Math.round((outreachSuccesses / outreachResolved) * 100)}%` : '—';
  if (outreachSuccessDetail) outreachSuccessDetail.innerText = outreachResolved ? `${outreachSuccesses} successful of ${outreachResolved} resolved` : 'no resolved outreach today';

  const setQueueMetric = (valueId, detailId, pending, prepared, label) => {
    const value = document.getElementById(valueId);
    const detail = document.getElementById(detailId);
    if (value) value.innerText = pending;
    if (detail) detail.innerText = pending ? `${pending} ${label}${pending === 1 ? '' : 's'} awaiting action` : (prepared ? `${prepared} ${label}${prepared === 1 ? '' : 's'} already resolved` : 'nothing queued today');
  };
  document.getElementById('overviewFollowupMetric')?.classList.remove('disabled');
  setQueueMetric('overviewFollowupDueValue', 'overviewFollowupDueDetail', Number(stats.followups?.pending || 0), Number(stats.followups?.prepared || 0), 'follow-up');
  const withdrawalPrepared = Number(stats.withdrawals?.prepared || 0);
  const withdrawalCompleted = Number(stats.withdrawals?.completed || 0);
  const withdrawalValue = document.getElementById('overviewWithdrawalDueValue');
  const withdrawalDetail = document.getElementById('overviewWithdrawalDueDetail');
  if (withdrawalValue) withdrawalValue.innerText = withdrawalPrepared;
  if (withdrawalDetail) withdrawalDetail.innerText = withdrawalPrepared ? `${withdrawalCompleted} completed today · ${Math.max(0, withdrawalPrepared - withdrawalCompleted)} remaining` : 'nothing due today';

  const watcher = stats.watcher || {};
  const watcherRateValue = document.getElementById('overviewWatcherRateValue');
  const watcherRateDetail = document.getElementById('overviewWatcherRateDetail');
  if (watcherRateValue) watcherRateValue.innerText = watcher.finalized_checkpoints ? `${watcher.completion_rate}%` : '—';
  if (watcherRateDetail) watcherRateDetail.innerText = watcher.finalized_checkpoints ? `${watcher.completed_checkpoints} of ${watcher.finalized_checkpoints} finalized tasks completed` : 'no finalized watcher tasks yet';
  const watcherAlert = document.getElementById('overviewWatcherAlert');
  const activeAlerts = Array.isArray(watcher.alerts) ? watcher.alerts : [];
  if (watcherAlert) {
    watcherAlert.hidden = activeAlerts.length === 0;
    watcherAlert.innerText = activeAlerts.length
      ? `${activeAlerts.slice(0, 3).map(alert => {
          const retryAt = alert.next_retry_at ? ` until ${new Date(alert.next_retry_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '';
          const reason = String(alert.detail || alert.reason || 'checkpoint failed').replaceAll('_', ' ');
          if (String(alert.reason || '').includes('quarantined_profile_issues')) {
            return `Activity queue: ${reason}`;
          }
          return `Watcher status: ${alert.workflow} — ${reason}${retryAt}`;
        }).join(' · ')}${activeAlerts.length > 3 ? ` · +${activeAlerts.length - 3} more` : ''}`
      : '';
  }
  renderOverviewWorkflow(stats);

  // Update real-time timers based on session start/last event times
  const updateTimer = (timerId, start_time, last_event_time, is_done, is_running) => {
    const timerEl = document.getElementById(timerId);
    if (!start_time) {
      timerEl.innerText = '00m 00s';
      return;
    }
    const startMs = new Date(start_time).getTime();
    let endMs = Date.now();
    if (is_done && last_event_time) {
      endMs = new Date(last_event_time).getTime();
    } else if (!is_running && last_event_time) {
      endMs = new Date(last_event_time).getTime();
    }
    const elapsed = Math.floor((endMs - startMs) / 1000);
    timerEl.innerText = formatElapsedTime(Math.max(0, Math.min(elapsed, 86400))); // Cap at 24h
  };

  updateTimer('actMonitorTimer', stats.activity.start_time, stats.activity.last_event_time, stats.activity_run_done, stats.activity.is_running);
  updateTimer('fuMonitorTimer', stats.followups.start_time, stats.followups.last_event_time, stats.followup_daytime_done || stats.followup_evening_done, stats.followups.is_running);
  updateTimer('wdMonitorTimer', stats.withdrawals.start_time, stats.withdrawals.last_event_time, stats.withdrawal_execute_done, stats.withdrawals.is_running);

  // 3. Update monitor text fields & chart visual fills
  // Activity Check
  actMonitorProgress.innerText = `${stats.activity.completed} / ${stats.activity.prepared} resolved`;
  if (stats.activity.prepared > 0) {
    const pct = Math.round((stats.activity.completed / stats.activity.prepared) * 100);
    actChartFill.style.width = `${pct}%`;
  } else {
    actChartFill.style.width = '0%';
  }

  // Update Activity Pacing countdown in real-time status label
  if (stats.activity.is_running && stats.activity.next_action_due) {
    const now = Date.now();
    const due = new Date(stats.activity.next_action_due).getTime();
    if (due > now) {
      const waitSec = Math.floor((due - now) / 1000);
      actMonitorStatus.innerText = `Pacing (${waitSec}s)`;
    } else {
      actMonitorStatus.innerText = 'Running';
    }
  } else {
    actMonitorStatus.innerText = stats.activity.is_running ? 'Running' : 'Idle';
    actMonitorStatus.className = 'monitor-val ' + (stats.activity.is_running ? 'running' : 'idle');
  }

  // Follow-ups
  const followupInsights = [
    ['fuDueCount', stats.followups?.due_today ?? stats.followups?.pending ?? 0],
    ['fuFirstMessageCount', stats.followups?.first_message_drafts ?? 0],
    ['fuSecondMessageCount', stats.followups?.second_message_due ?? 0],
    ['fuPipelineCount', stats.followups?.pipeline_assessed ?? 0],
  ];
  followupInsights.forEach(([id, value]) => { const element = document.getElementById(id); if (element) element.innerText = value; });
  fuMonitorProgress.innerText = `${stats.followups.completed} / ${stats.followups.prepared} dispatched`;
  if (stats.followups.prepared > 0) {
    const pct = Math.round((stats.followups.completed / stats.followups.prepared) * 100);
    fuChartFill.style.width = `${pct}%`;
  } else {
    fuChartFill.style.width = '0%';
  }

  if (!stats.followups.enabled) {
    fuMonitorStatus.innerText = 'Paused';
    fuMonitorStatus.className = 'monitor-val idle';
  } else if (stats.followups.is_running && stats.followups.next_action_due) {
    const now = Date.now();
    const due = new Date(stats.followups.next_action_due).getTime();
    if (due > now) {
      const waitSec = Math.floor((due - now) / 1000);
      fuMonitorStatus.innerText = `Pacing (${waitSec}s)`;
    } else {
      fuMonitorStatus.innerText = 'Running';
    }
  } else {
    fuMonitorStatus.innerText = stats.followups.is_running ? 'Running' : 'Idle';
    fuMonitorStatus.className = 'monitor-val ' + (stats.followups.is_running ? 'running' : 'idle');
  }

  // Withdrawals
  wdMonitorProgress.innerText = `${stats.withdrawals.completed} / ${stats.withdrawals.prepared} withdrawn`;
  if (stats.withdrawals.prepared > 0) {
    const pct = Math.round((stats.withdrawals.completed / stats.withdrawals.prepared) * 100);
    wdChartFill.style.width = `${pct}%`;
  } else {
    wdChartFill.style.width = '0%';
  }
  wdMonitorStatus.innerText = stats.withdrawals.is_running ? 'Running' : 'Idle';
  wdMonitorStatus.className = 'monitor-val ' + (stats.withdrawals.is_running ? 'running' : 'idle');
  if (currentCategory === 'withdrawal' && currentRunningTaskElement && !stats.withdrawals.is_running) {
    appendToConsole('[Withdrawal] Local action finished.', 'success');
    finishRunningTask(0);
  }

  const wdCooldownVal = document.getElementById('wdCooldownVal');
  if (stats.withdrawals.cooldown_active) {
    wdCooldownVal.style.display = 'inline-block';
    if (stats.withdrawals.cooldown_until) {
      const resetTime = new Date(stats.withdrawals.cooldown_until);
      wdCooldownVal.innerText = `Cooldown until ${resetTime.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
    } else {
      wdCooldownVal.innerText = `Cooldown active`;
    }
  } else {
    wdCooldownVal.style.display = 'none';
  }
  renderWithdrawalState(stats.withdrawals);
  renderDailyCompletionHistory(stats.watcher?.history || []);

  // Populate Active Queue list
  if (queueList) {
  queueList.innerHTML = '';
  const combinedQueue = [];

  stats.activity.list.forEach(t => combinedQueue.push({ ...t, type: 'Activity', colorClass: 'pending' }));
  stats.followups.list.forEach(t => combinedQueue.push({ ...t, type: 'Followup', colorClass: 'pending' }));
  stats.withdrawals.list.forEach(t => combinedQueue.push({ ...t, type: 'Withdrawal', colorClass: 'pending' }));

  combinedQueue.sort((a, b) => {
    const aIsPending = !['Success', 'Replied', 'Unsure', 'Done', 'Failed'].includes(a.status);
    const bIsPending = !['Success', 'Replied', 'Unsure', 'Done', 'Failed'].includes(b.status);
    if (aIsPending && !bIsPending) return -1;
    if (!aIsPending && bIsPending) return 1;
    return 0;
  });

  if (combinedQueue.length === 0) {
    queueList.innerHTML = `<div class="empty-state">No prepared session queue found for today (${stats.today}). Run a Prepare task under controls to populate.</div>`;
  } else {
    const escapeHtml = (unsafe) => (unsafe || '').toString().replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");

    combinedQueue.forEach(item => {
      let indicatorClass = 'pending';
      const statusLower = item.status.toLowerCase();
      if (['success', 'replied', 'done', 'withdrawn', 'sent'].some(s => statusLower.includes(s))) {
        indicatorClass = 'completed';
      } else if (['fail', 'unsure', 'error', 'blocked'].some(s => statusLower.includes(s))) {
        indicatorClass = 'failed';
      }

      const card = document.createElement('div');
      card.className = 'queue-card';
      const hasMessage = !!item.message;
      const expandIcon = hasMessage ? `<svg class="expand-icon" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#a0aec0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-left: 4px;"><polyline points="6 9 12 15 18 9"></polyline></svg>` : '';

      const itemKey = `${item.name}-${item.type}-${item.detail}`;
      const isExpanded = expandedQueueItems.has(itemKey);

      card.innerHTML = `
        <div class="queue-card-header">
          <div class="queue-card-indicator ${indicatorClass}"></div>
          <div class="queue-card-content">
            <div class="queue-card-name">${escapeHtml(item.name)}</div>
            <div class="queue-card-company">${escapeHtml(item.company)}</div>
          </div>
          <div class="queue-card-badge">${escapeHtml(item.type)} (${escapeHtml(item.detail)})</div>
          ${expandIcon}
        </div>
        ${hasMessage ? `<div class="queue-card-message${isExpanded ? ' expanded' : ''}">${escapeHtml(item.message)}</div>` : ''}
      `;

      if (hasMessage) {
        card.addEventListener('click', () => {
          const msgEl = card.querySelector('.queue-card-message');
          const nowExpanded = msgEl.classList.toggle('expanded');
          if (nowExpanded) {
            expandedQueueItems.add(itemKey);
          } else {
            expandedQueueItems.delete(itemKey);
          }
        });
      }
      queueList.appendChild(card);
    });
  }
  }
}

function renderDailyCompletionHistory(history) {
  const body = document.getElementById('dailyCompletionHistoryBody');
  if (!body) return;
  if (!history.length) {
    body.innerHTML = '<tr><td colspan="5"><div class="empty-state">No watcher completion history yet.</div></td></tr>';
    return;
  }
  body.innerHTML = history.slice(0, 14).map(day => `<tr><td>${escapeHtmlSafe(day.date)}</td><td><strong>${day.completed}</strong></td><td>${day.scheduled}</td><td>${day.rate}%</td><td><span class="obf-status-pill ${day.outcome === 'Completed' ? 'success' : (day.outcome === 'In progress' ? 'warning' : 'neutral')}">${escapeHtmlSafe(day.outcome)}</span></td></tr>`).join('');
}

async function loadHistory() {
  const historyList = document.getElementById('historyList');
  if (!historyList) return;

  historyList.innerHTML = '<div class="empty-state">Loading history...</div>';

  const history = await ipcRenderer.invoke('read-history');

  if (!history || history.length === 0) {
    historyList.innerHTML = '<div class="empty-state">No historical records found.</div>';
    return;
  }

  historyList.innerHTML = '';

  history.forEach(record => {
    const card = document.createElement('div');
    card.className = 'history-card';

    const actStr = record.activity ? `${record.activity.completed} / ${record.activity.total} resolved` : 'No data';
    const fuStr = record.followups ? `${record.followups.completed} / ${record.followups.total} dispatched` : 'No data';
    const wdStr = record.withdrawals ? `${record.withdrawals.completed} / ${record.withdrawals.total} withdrawn` : 'No data';

    card.innerHTML = `
      <div class="history-date-header">${record.date}</div>
      <div class="history-stats-row">
        <span class="history-stats-label">Activity Check:</span>
        <span class="history-stats-val">${actStr}</span>
      </div>
      <div class="history-stats-row">
        <span class="history-stats-label">Follow-ups:</span>
        <span class="history-stats-val">${fuStr}</span>
      </div>
      <div class="history-stats-row">
        <span class="history-stats-label">Withdrawals:</span>
        <span class="history-stats-val">${wdStr}</span>
      </div>
    `;
    historyList.appendChild(card);
  });
}

// Format seconds into MMm SSs (e.g. 02m 45s)
function formatElapsedTime(secs) {
  const m = Math.floor(secs / 60).toString().padStart(2, '0');
  const s = (secs % 60).toString().padStart(2, '0');
  return `${m}m ${s}s`;
}

function getCategoryCode(category) {
  if (category === 'activity') return 'act';
  if (category === 'followup') return 'fu';
  if (category === 'withdrawal') return 'wd';
  return category;
}

// Start stopwatch timer (UI placeholder)
function startStopwatch(category) {
  const code = getCategoryCode(category);
  const statusElement = document.getElementById(`${code}MonitorStatus`);

  if (statusElement) {
    statusElement.innerText = 'Running';
    statusElement.className = 'monitor-val running';
  }
}

// Stop stopwatch timer (UI placeholder)
function stopStopwatch(category = '', finalState = '') {
  if (category && finalState) {
    const code = getCategoryCode(category);
    const statusElement = document.getElementById(`${code}MonitorStatus`);
    if (statusElement) {
      statusElement.innerText = finalState;
      statusElement.className = `monitor-val ${finalState.toLowerCase()}`;
    }
  }
}

// Append logs to stdout console
function appendToConsole(text, type = '') {
  const line = document.createElement('div');
  if (type) line.className = `${type}-message`;
  line.innerText = text;
  consoleLog.appendChild(line);

  if (autoScrollCheck.checked) {
    consoleLog.scrollTop = consoleLog.scrollHeight;
  }
}

// Console Panel Toggling (Height expansion/collapsing)
toggleConsoleBtn.addEventListener('click', () => {
  isConsoleExpanded = !isConsoleExpanded;
  if (isConsoleExpanded) {
    consolePanel.classList.add('expanded');
    toggleConsoleBtn.classList.add('console-toggle-expanded');
  } else {
    consolePanel.classList.remove('expanded');
    toggleConsoleBtn.classList.remove('console-toggle-expanded');
  }
});

clearConsoleBtn.addEventListener('click', () => {
  consoleLog.innerHTML = '<div class="system-message">Console log cleared.</div>';
});

function jobDiscoveryTone(status = '') {
  const normalized = String(status).toLowerCase();
  if (normalized.includes('fail') || normalized.includes('error')) return 'danger';
  if (normalized.includes('running') || normalized.includes('pending')) return 'warning';
  if (normalized.includes('complete') || normalized === 'active') return 'success';
  return 'neutral';
}

function jobDiscoveryFact(label, value) {
  return `<div><span>${escapeHtmlSafe(label)}</span><strong title="${escapeHtmlSafe(String(value ?? '—'))}">${escapeHtmlSafe(String(value ?? '—'))}</strong></div>`;
}

function renderJobDiscoveryDashboard(data) {
  const config = data.config || {};
  const status = data.status || {};
  const counts = status.jobStatusCounts || {};
  const recentRuns = Array.isArray(status.recentRuns) ? status.recentRuns : [];
  const recent = recentRuns[0] || {};
  const activeJobs = Number(counts.active || 0) + Number(counts.review || 0);
  const reviewJobs = Number(counts.review || 0);
  const runTitle = document.getElementById('jobDiscoveryStatusTitle');
  const runDetail = document.getElementById('jobDiscoveryStatusDetail');
  const pill = document.getElementById('jobDiscoveryStatusPill');
  const feedback = document.getElementById('jobDiscoveryFeedback');
  const phase = !data.available ? 'Unavailable' : data.is_running ? 'Running' : config.automationEnabled ? 'Ready for daily run' : 'Manual mode';
  const detail = !data.available
    ? (data.error || 'The service directory is unavailable.')
    : data.error
      ? `Status could not be read: ${data.error}`
      : data.is_running
        ? `Active run ${status.activeRunId || data.lock?.runId || ''} is checkpointing its progress locally.`
        : config.automationEnabled
          ? `Automation is enabled for ${config.dailyRunTime || status.dailyRunTime || 'the configured time'} ${config.timezone || status.timezone || ''}.`
          : 'Automatic runs are disabled; use Run discovery now whenever you want to collect jobs.';
  runTitle.innerText = phase;
  runDetail.innerText = detail;
  pill.className = `obf-status-pill ${!data.available || data.error ? 'danger' : data.is_running ? 'active' : config.automationEnabled ? 'success' : 'neutral'}`;
  pill.innerText = phase;
  document.getElementById('jobDiscoveryUpdated').innerText = data.last_updated ? `Updated ${formatObfTime(data.last_updated, true)}` : '—';
  document.getElementById('jobDiscoveryJobsMetric').innerText = activeJobs;
  document.getElementById('jobDiscoveryJobsDetail').innerText = `${Number(counts.active || 0)} active · ${reviewJobs} review`;
  document.getElementById('jobDiscoveryReviewMetric').innerText = reviewJobs;
  document.getElementById('jobDiscoveryHydratedMetric').innerText = Number(recent.hydrated || 0);
  document.getElementById('jobDiscoveryHydratedDetail').innerText = recent.id ? `${titleCaseStatusClient(recent.status || 'recorded')} · ${recent.newJobs || 0} new` : 'no completed run yet';
  document.getElementById('jobDiscoveryCursorMetric').innerText = Number(status.queryCursor || 0) + 1;
  document.getElementById('jobDiscoveryCursorDetail').innerText = `${Number(status.enabledQueries || 0)} enabled queries`;
  document.getElementById('jobDiscoveryHealthMetric').innerText = recent.status ? titleCaseStatusClient(recent.status) : '—';
  document.getElementById('jobDiscoveryHealthDetail').innerText = recent.endedAt ? `Finished ${formatObfTime(recent.endedAt, true)}` : 'awaiting first run';

  const toggle = document.getElementById('jobDiscoveryEnabledToggle');
  toggle.setAttribute('aria-checked', String(Boolean(config.automationEnabled)));
  toggle.disabled = !data.available;
  document.getElementById('jobDiscoveryEnabledDetail').innerText = config.automationEnabled
    ? `Enabled · runs daily at ${config.dailyRunTime || status.dailyRunTime || '—'}`
    : 'Disabled · manual runs only';
  const schedulerButton = document.getElementById('jobDiscoverySchedulerBtn');
  schedulerButton.disabled = !data.available;
  schedulerButton.innerText = data.scheduler_enabled ? 'Stop scheduler' : 'Start scheduler';
  document.getElementById('jobDiscoverySchedulerState').innerText = data.scheduler_enabled ? 'Running as a local LaunchAgent' : 'Not running';
  const editing = Boolean(document.activeElement?.closest?.('.job-discovery-settings-grid'));
  if (!editing) {
    document.getElementById('jobDiscoveryRunTime').value = config.dailyRunTime || status.dailyRunTime || '08:00';
    document.getElementById('jobDiscoveryMaxQueries').value = config.maxQueriesPerRun || 180;
    document.getElementById('jobDiscoveryMaxListings').value = config.maxListingsPerRun || 180;
  }
  document.getElementById('jobDiscoveryRunBtn').disabled = !data.available || Boolean(data.is_running);
  document.getElementById('jobDiscoveryReverifyBtn').disabled = !data.available || Boolean(data.is_running);
  document.getElementById('jobDiscoveryRefreshBtn').disabled = !data.available;
  if (feedback && data.error) feedback.innerText = data.error;

  document.getElementById('jobDiscoveryFacts').innerHTML = [
    jobDiscoveryFact('Active run', status.activeRunId || data.lock?.runId || 'None'),
    jobDiscoveryFact('Last scheduled day', status.lastScheduledDate || 'Not yet run'),
    jobDiscoveryFact('Database', status.database?.databasePath || 'Not initialized'),
    jobDiscoveryFact('Records', `${status.database?.jobs || 0} jobs · ${status.database?.companies || 0} companies`)
  ].join('');
  document.getElementById('jobDiscoveryDiagnostics').innerHTML = [
    jobDiscoveryFact('Service root', data.root || '—'),
    jobDiscoveryFact('Control source', status.controlSource || 'local'),
    jobDiscoveryFact('Configuration', status.configurationSource || 'local'),
    jobDiscoveryFact('Run lock', data.lock?.heartbeatAt || 'Clear')
  ].join('');
  const progressCounts = status.queryProgressCounts || {};
  const progressEntries = Object.entries(progressCounts).sort(([left], [right]) => left.localeCompare(right));
  document.getElementById('jobDiscoveryProgress').innerHTML = progressEntries.length
    ? progressEntries.map(([state, count]) => `<div class="job-discovery-progress ${jobDiscoveryTone(state)}"><span>${escapeHtmlSafe(titleCaseStatusClient(state))}</span><strong>${Number(count)} queries</strong></div>`).join('')
    : '<div class="empty-state">No query checkpoints have been recorded yet.</div>';
  document.getElementById('jobDiscoveryHistoryBody').innerHTML = recentRuns.length
    ? recentRuns.map(run => `<tr><td>${run.startedAt ? escapeHtmlSafe(formatObfTime(run.startedAt, true)) : '—'}</td><td>${escapeHtmlSafe(titleCaseStatusClient(run.trigger || 'manual'))}</td><td><span class="obf-status-pill ${jobDiscoveryTone(run.status)}">${escapeHtmlSafe(titleCaseStatusClient(run.status || 'unknown'))}</span></td><td>${Number(run.queriesCompleted || 0)} / ${Number(run.queriesAttempted || 0)}</td><td>${Number(run.hydrated || 0)}</td><td>${Number(run.newJobs || 0)}</td><td>${escapeHtmlSafe(run.stopReason || run.errors?.[0] || '—')}</td></tr>`).join('')
    : '<tr><td colspan="7"><div class="empty-state">No discovery runs have been recorded yet.</div></td></tr>';
}

async function refreshJobDiscoveryDashboard() {
  try {
    const data = await ipcRenderer.invoke('read-job-discovery-dashboard');
    if (data) renderJobDiscoveryDashboard(data);
  } catch (error) {
    const feedback = document.getElementById('jobDiscoveryFeedback');
    if (feedback) feedback.innerText = `Could not refresh Job Discovery: ${error.message}`;
  }
}

function readJobDiscoverySettings(enabledOverride = null) {
  const toggle = document.getElementById('jobDiscoveryEnabledToggle');
  return {
    automationEnabled: enabledOverride === null ? toggle.getAttribute('aria-checked') === 'true' : enabledOverride,
    dailyRunTime: document.getElementById('jobDiscoveryRunTime').value,
    maxQueriesPerRun: Number(document.getElementById('jobDiscoveryMaxQueries').value),
    maxListingsPerRun: Number(document.getElementById('jobDiscoveryMaxListings').value)
  };
}

async function saveJobDiscoverySettings(enabledOverride = null) {
  const settings = readJobDiscoverySettings(enabledOverride);
  if (!settings.dailyRunTime || settings.maxQueriesPerRun < 1 || settings.maxListingsPerRun < 1) {
    appendToConsole('Set a valid daily time and positive limits for Job Discovery.', 'error');
    return false;
  }
  const result = await ipcRenderer.invoke('save-job-discovery-settings', settings);
  if (!result?.ok) {
    appendToConsole(result?.error || 'Could not save Job Discovery settings.', 'error');
    await refreshJobDiscoveryDashboard();
    return false;
  }
  document.getElementById('jobDiscoveryFeedback').innerText = 'Runtime controls saved locally.';
  appendToConsole('Job Discovery runtime controls saved.', 'success');
  await refreshJobDiscoveryDashboard();
  return true;
}

async function startJobDiscoveryAction(action, button) {
  if (!isElectronRuntime) {
    const channel = action === 'run' ? 'start-job-discovery-run' : 'start-job-discovery-reverify';
    const result = await ipcRenderer.invoke(channel, {});
    appendToConsole(result?.ok ? `Job Discovery ${action} started in the background.` : (result?.error || 'Could not start Job Discovery.'), result?.ok ? 'success' : 'error');
    await refreshJobDiscoveryDashboard();
    return;
  }
  if (currentRunningTaskElement) {
    appendToConsole('Please wait or cancel the active running process before starting Job Discovery.', 'error');
    return;
  }
  currentRunningTaskElement = button;
  currentCategory = 'job-discovery';
  startStopwatch(currentCategory);
  consoleStatusIndicator.className = 'status-indicator running';
  consoleStatusText.innerText = `Running Job Discovery: ${action}`;
  stopProcessBtn.style.display = 'block';
  appendToConsole(`>>> Launching Daily Job Discovery: ${action}`, 'system');
  ipcRenderer.send('run-job-discovery-command', { action });
  await refreshJobDiscoveryDashboard();
}

document.getElementById('jobDiscoveryRefreshBtn')?.addEventListener('click', refreshJobDiscoveryDashboard);
document.getElementById('jobDiscoverySaveBtn')?.addEventListener('click', () => saveJobDiscoverySettings());
document.getElementById('jobDiscoveryEnabledToggle')?.addEventListener('click', event => saveJobDiscoverySettings(event.currentTarget.getAttribute('aria-checked') !== 'true'));
document.getElementById('jobDiscoverySchedulerBtn')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const enabled = button.innerText === 'Start scheduler';
    const result = await ipcRenderer.invoke('toggle-job-discovery-scheduler', { enabled });
    if (!result?.ok) throw new Error(result?.error || 'Scheduler service could not be changed.');
    appendToConsole(`Job Discovery scheduler ${result.enabled ? 'started' : 'stopped'}.`, 'success');
  } catch (error) {
    appendToConsole(error.message, 'error');
  } finally {
    await refreshJobDiscoveryDashboard();
  }
});
document.getElementById('jobDiscoveryRunBtn')?.addEventListener('click', event => startJobDiscoveryAction('run', event.currentTarget));
document.getElementById('jobDiscoveryReverifyBtn')?.addEventListener('click', event => startJobDiscoveryAction('reverify', event.currentTarget));

// Run command execution trigger helper
function triggerTaskExecution(scriptPath, args, buttonEl) {
  if (currentRunningTaskElement) {
    appendToConsole('Error: A task is already running in the background. Stop it first.', 'error');
    return;
  }

  currentRunningTaskElement = buttonEl;

  // Identify category from button ancestor
  const sectionEl = buttonEl.closest('.category-section');
  currentCategory = sectionEl
    ? sectionEl.id.replace('section-', '')
    : buttonEl.closest('[data-page="obf"]')
      ? 'obf'
      : buttonEl.closest('[data-page="lead-prep"]')
        ? 'lead-prep'
        : 'system';

  // Start active stopwatch
  startStopwatch(currentCategory);

  // Toggle Console status text and indicator
  consoleStatusIndicator.className = 'status-indicator running';
  consoleStatusText.innerText = `Running: python3 ${scriptPath.split('/').pop()}`;
  stopProcessBtn.style.display = 'block';

  // Log start line
  appendToConsole(`>>> Launching: python3 ${scriptPath} ${args.join(' ')}`, 'system');

  ipcRenderer.send('run-command', { scriptPath, args });
}

document.getElementById('obfPrepBtn')?.addEventListener('click', event => {
  const today = new Date();
  const dateValue = `${today.getMonth() + 1}/${today.getDate()}/${today.getFullYear()}`;
  triggerTaskExecution(
    'helpers/linkedin_outreach_session.py',
    ['prepare-8_30-session', '--date', dateValue],
    event.currentTarget
  );
  refreshObfDashboard();
});

document.getElementById('obfExecBtn')?.addEventListener('click', event => {
  const today = new Date();
  const dateValue = `${today.getMonth() + 1}/${today.getDate()}/${today.getFullYear()}`;
  triggerTaskExecution(
    'scripts/run_outreach_lanes.py',
    [
      '--date',
      dateValue,
      '--max-sends',
      String(Number(document.getElementById('obfMaxSends')?.value || 30))
    ],
    event.currentTarget
  );
  refreshObfDashboard();
});

document.getElementById('leadPrepRunBtn')?.addEventListener('click', event => {
  const volume = Math.max(1, Number(document.getElementById('leadPrepVolume')?.value || 60));
  const mode = document.getElementById('leadPrepOverlapScanMode')?.value || 'auto';
  const topUpMode = document.getElementById('leadPrepTopUpMode')?.value || 'auto';
  const args = ['prepare-review', '--limit', String(volume)];
  if (mode === 'off') args.push('--disable-overlap-scan');
  if (topUpMode === 'off') args.push('--disable-overlap-top-ups');
  triggerTaskExecution('scripts/lead_exec_research.py', args, event.currentTarget);
  refreshLeadPrepDashboard();
});

function startWithdrawalFromDashboard(action, button) {
  if (currentRunningTaskElement) {
    appendToConsole('Please wait or cancel the active running process before running another.', 'error');
    return;
  }
  currentRunningTaskElement = button;
  currentCategory = 'withdrawal';
  startStopwatch(currentCategory);
  consoleStatusIndicator.className = 'status-indicator running';
  consoleStatusText.innerText = `Starting withdrawal ${action}...`;
  stopProcessBtn.style.display = 'block';
  appendToConsole(`>>> Starting withdrawal ${action}`, 'system');
  ipcRenderer.invoke(`start-withdrawal-${action}`, {})
    .then(result => {
      if (!result?.ok) throw new Error(result?.error || 'Withdrawal action did not start.');
      appendToConsole(`[Withdrawal] ${action} started for ${result.date}.`, 'success');
      refreshStats();
    })
    .catch(error => {
      appendToConsole(`[Withdrawal] ${error.message}`, 'error');
      finishRunningTask(1);
    });
}

document.getElementById('btn-wd-prep')?.addEventListener('click', event => startWithdrawalFromDashboard('prepare', event.currentTarget));
document.getElementById('btn-wd-run')?.addEventListener('click', event => startWithdrawalFromDashboard('execute', event.currentTarget));
document.getElementById('btn-wd-full')?.addEventListener('click', event => startWithdrawalFromDashboard('full', event.currentTarget));
document.querySelector('#section-withdrawal .btn-stop-run')?.addEventListener('click', async event => {
  event.stopImmediatePropagation();
  try {
    const result = await ipcRenderer.invoke('stop-withdrawal', {});
    if (!result?.ok) throw new Error(result?.error || 'Withdrawal action could not be stopped.');
    appendToConsole('[Withdrawal] Stop signal sent.', 'error');
  } catch (error) {
    appendToConsole(`[Withdrawal] ${error.message}`, 'error');
  }
});

// Bind click events to all trigger buttons
document.querySelectorAll('.trigger-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (['btn-wd-prep', 'btn-wd-run', 'btn-wd-full'].includes(btn.id)) return;
    const scriptPath = btn.getAttribute('data-script');
    const argsString = btn.getAttribute('data-args');
    const multiScriptsString = btn.getAttribute('data-multi-scripts');
    const requiresLimit = btn.getAttribute('data-requires-limit');

    if (currentRunningTaskElement) {
      appendToConsole('Please wait or cancel the active running process before running another.', 'error');
      return;
    }

    if (multiScriptsString) {
      const chain = JSON.parse(multiScriptsString);
      if (requiresLimit === 'true') {
        openParameterDrawer('Prepare Limit', (limit) => {
          scriptChain = chain.map((step, idx) => {
            return {
              path: step.path,
              args: idx === 1 ? [...step.args, '--limit', limit] : step.args
            };
          });
          executeNextChainStep(btn);
        });
      } else {
        scriptChain = [...chain];
        executeNextChainStep(btn);
      }
      return;
    }

    let parsedArgs = JSON.parse(argsString);

    if (requiresLimit === 'true') {
      openParameterDrawer('Set Limit Count', (limit) => {
        parsedArgs.push('--limit', limit);
        triggerTaskExecution(scriptPath, parsedArgs, btn);
      });
    } else if (requiresLimit === 'optional') {
      openParameterDrawer('Limit Count (Optional)', (limit) => {
        if (limit && limit.trim() !== '') {
          parsedArgs.push('--limit', limit);
        }
        triggerTaskExecution(scriptPath, parsedArgs, btn);
      }, true);
    } else {
      triggerTaskExecution(scriptPath, parsedArgs, btn);
    }
  });
});

// Chained Scripts sequential executor
function executeNextChainStep(btn) {
  if (scriptChain.length === 0) {
    finishRunningTask(0);
    return;
  }

  const nextStep = scriptChain.shift();
  triggerTaskExecution(nextStep.path, nextStep.args, btn);
}

// Parameter Drawer dialog
function openParameterDrawer(title, callback, isOptional = false) {
  paramTitle.innerText = title;
  paramLimit.value = isOptional ? '' : '5';
  paramLimitLabel.innerText = isOptional ? 'Limit Count (leave empty for all):' : 'Limit Count:';

  paramDrawer.classList.add('open');

  const cleanup = () => {
    launchWithParamBtn.removeEventListener('click', onLaunch);
    closeParamBtn.removeEventListener('click', onClose);
    paramDrawer.classList.remove('open');
  };

  const onLaunch = () => {
    const val = paramLimit.value;
    if (!isOptional && (!val || isNaN(val) || parseInt(val) < 1)) {
      alert('Please enter a valid target limit count.');
      return;
    }
    cleanup();
    callback(val);
  };

  const onClose = () => {
    cleanup();
  };

  launchWithParamBtn.addEventListener('click', onLaunch);
  closeParamBtn.addEventListener('click', onClose);
}

// Stop execution
stopProcessBtn.addEventListener('click', () => {
  ipcRenderer.send('stop-command');
  appendToConsole('>>> Sending termination signal...', 'error');
});

// IPC log listener
ipcRenderer.on('command-output', (event, data) => {
  appendToConsole(data);
});

ipcRenderer.on('command-error', (event, message) => {
  appendToConsole(`>>> ${message}`, 'error');
  if (currentRunningTaskElement) finishRunningTask(1);
});

// Handle Command Terminations
function finishRunningTask(code) {
  if (!currentRunningTaskElement) return;

  const category = currentCategory;

  if (code === 0) {
    stopStopwatch(category, 'Success');
    consoleStatusIndicator.className = 'status-indicator success';
    consoleStatusText.innerText = 'Task Completed Successfully';
    appendToConsole('>>> Task completed successfully.', 'success');

    // Play sound chime!
    playSuccessChime();

    // Refresh state report summaries (which updates checkboxes and progress bar)
    refreshStats();
    if (category === 'obf') refreshObfDashboard();
    if (category === 'lead-prep') refreshLeadPrepDashboard();
    if (category === 'job-discovery') refreshJobDiscoveryDashboard();
  } else {
    stopStopwatch(category, 'Error');
    consoleStatusIndicator.className = 'status-indicator error';
    consoleStatusText.innerText = `Task failed (Code: ${code})`;
    appendToConsole(`>>> Task exited with error code: ${code}`, 'error');

    // Refresh stats anyway
    refreshStats();
    if (category === 'obf') refreshObfDashboard();
    if (category === 'lead-prep') refreshLeadPrepDashboard();
    if (category === 'job-discovery') refreshJobDiscoveryDashboard();
  }

  stopProcessBtn.style.display = 'none';
  currentRunningTaskElement = null;
  currentCategory = '';
}

// IPC Exit listener
ipcRenderer.on('command-exit', (event, code) => {
  if (scriptChain.length > 0 && code === 0) {
    const element = currentRunningTaskElement;
    currentRunningTaskElement = null;
    executeNextChainStep(element);
  } else {
    scriptChain = []; // Clear chain
    finishRunningTask(code);
  }
});

// IPC Stopped listener
ipcRenderer.on('command-stopped', () => {
  appendToConsole('>>> Task terminated by user.', 'error');
  scriptChain = []; // Clear chain
  if (currentRunningTaskElement) {
    stopStopwatch(currentCategory, 'Idle');
    currentRunningTaskElement = null;
    currentCategory = '';
  }
  consoleStatusIndicator.className = 'status-indicator idle';
  consoleStatusText.innerText = 'Task Cancelled';
  stopProcessBtn.style.display = 'none';
});

// Slide-in / Out handler signals
ipcRenderer.on('slide-in', () => {
  appContainer.classList.add('open');
});

ipcRenderer.on('slide-out', () => {
  appContainer.classList.remove('open');
});

ipcRenderer.on('refresh-data', () => {
  refreshStats();
});

ipcRenderer.on('fullscreen-changed', (_event, isFullScreen) => {
  if (!fullScreenBtn) return;
  fullScreenBtn.title = isFullScreen ? 'Exit full screen' : 'Enter full screen';
  fullScreenBtn.setAttribute('aria-label', fullScreenBtn.title);
});

// Caffeinate UI Logic variables
let caffeineTimer = null;
let caffeineSecondsLeft = 0;
let isCaffeinated = false;

const caffeinateBtn = document.getElementById('caffeinateBtn');
const caffeinateDuration = document.getElementById('caffeinateDuration');
const launchChromeBtn = document.getElementById('launchChromeBtn');

function updateCaffeineButtonDisplay() {
  const overviewButton = document.getElementById('overviewCaffeinateBtn');
  if (isCaffeinated) {
    caffeinateBtn.classList.add('active-caffeine');
    if (caffeineSecondsLeft > 0) {
      const m = Math.floor(caffeineSecondsLeft / 60);
      const s = caffeineSecondsLeft % 60;
      caffeinateBtn.innerHTML = `<span class="btn-icon">☕</span> ${m}m ${s}s`;
    } else {
      caffeinateBtn.innerHTML = `<span class="btn-icon">☕</span> Indefinite`;
    }
    if (overviewButton) overviewButton.innerText = 'Caffeinate: ON';
  } else {
    caffeinateBtn.classList.remove('active-caffeine');
    caffeinateBtn.innerHTML = `<span class="btn-icon">☕</span> Caffeinate`;
    if (overviewButton) overviewButton.innerText = 'Caffeinate';
  }
}

caffeinateBtn.addEventListener('click', () => {
  if (isCaffeinated) {
    ipcRenderer.send('stop-caffeinate');
    clearCaffeineTimer();
  } else {
    const durationVal = caffeinateDuration.value;
    ipcRenderer.send('start-caffeinate', { durationSeconds: durationVal });

    isCaffeinated = true;
    if (durationVal !== 'infinite') {
      caffeineSecondsLeft = parseInt(durationVal, 10);
      startCaffeineCountdown();
    } else {
      caffeineSecondsLeft = 0;
    }
    updateCaffeineButtonDisplay();
  }
});

function startCaffeineCountdown() {
  if (caffeineTimer) clearInterval(caffeineTimer);
  caffeineTimer = setInterval(() => {
    caffeineSecondsLeft--;
    if (caffeineSecondsLeft <= 0) {
      clearCaffeineTimer();
    } else {
      updateCaffeineButtonDisplay();
    }
  }, 5000);
}

function clearCaffeineTimer() {
  if (caffeineTimer) {
    clearInterval(caffeineTimer);
    caffeineTimer = null;
  }
  isCaffeinated = false;
  caffeineSecondsLeft = 0;
  updateCaffeineButtonDisplay();
}

ipcRenderer.on('caffeinate-stopped', () => {
  clearCaffeineTimer();
});

let postEngagementDashboard = null;

function peEmptyRow(columns, message) {
  return `<tr><td colspan="${columns}"><div class="empty-state">${escapeHtmlSafe(message)}</div></td></tr>`;
}

function peProfileLink(candidate) {
  const rawName = String(candidate.name || '').trim();
  const name = escapeHtmlSafe(rawName && !/^https?:\/\//i.test(rawName) ? rawName : 'LinkedIn member');
  let url = '';
  try {
    const parsed = new URL(candidate.profile_url);
    if (parsed.protocol === 'https:' && /(^|\.)linkedin\.com$/i.test(parsed.hostname)) url = parsed.href;
  } catch (_) {}
  return `<div class="pe-profile-cell"><strong>${name}</strong>${url ? `<a href="${escapeHtmlSafe(url)}" target="_blank" rel="noopener noreferrer">View profile ↗</a>` : '<small>Profile unavailable</small>'}</div>`;
}

function renderPostEngagement(data) {
  postEngagementDashboard = data;
  const campaign = data.campaign || {};
  const config = data.config || {};
  const candidates = campaign.candidates || [];
  const sources = campaign.sources || [];
  const status = String(campaign.status || 'waiting_for_source');
  const execution = campaign.execution || {};
  document.getElementById('peExecutionProfile').innerText = execution.profile_name || '—';
  document.getElementById('peExecutionAction').innerText = data.is_running ? (execution.action || titleCaseStatusClient(campaign.stage || 'starting')) : `${titleCaseStatusClient(status)}${execution.action ? ` · last: ${execution.action}` : ''}`;
  document.getElementById('peExecutionMethod').innerText = execution.navigation_method || '—';
  document.getElementById('peExecutionReason').innerText = execution.fallback_reason || '—';
  const executionTime = execution.updated_at ? new Date(execution.updated_at) : null;
  document.getElementById('peExecutionUpdated').innerText = executionTime && !Number.isNaN(executionTime.valueOf())
    ? `Last decision ${executionTime.toLocaleString()}${data.is_running ? '' : ' · runner stopped'}` : 'No execution recorded';
  const needsSource = status === 'needs_another_post' || status === 'dry_run_needs_another_post';
  document.getElementById('peStatusTitle').innerText = needsSource ? 'Another source post is needed' : titleCaseStatusClient(status);
  document.getElementById('peStatusDetail').innerText = needsSource
    ? `${campaign.engagement_deficit || 0} engagement and ${campaign.connection_deficit || 0} connection slots remain.`
    : data.error || `Current stage: ${titleCaseStatusClient(campaign.stage || 'source_posts')} · ${titleCaseStatusClient(campaign.cdp_account || config.cdp_account || 'design')} CDP lane.`;
  const pill = document.getElementById('peStatusPill');
  pill.innerText = data.is_running ? 'Running' : needsSource ? 'Needs post' : titleCaseStatusClient(status);
  pill.className = `obf-status-pill ${data.is_running ? 'active' : needsSource ? 'warning' : status === 'completed' ? 'success' : 'neutral'}`;
  document.getElementById('peEngagedValue').innerText = `${campaign.engaged || 0} / ${campaign.target || '—'}`;
  document.getElementById('peEngagedDetail').innerText = campaign.preview_engaged && !campaign.engaged ? `${campaign.preview_engaged} would be engaged in a live run` : campaign.engagement_deficit ? `${campaign.engagement_deficit} more active profiles needed` : 'unique profiles with at least one new Like';
  const batches = campaign.engagement_batches || [];
  const dailyTarget = Math.max(0, Number(campaign.target) || 0);
  const dailyDone = Math.max(0, Number(campaign.engaged) || 0);
  const percent = dailyTarget ? Math.min(100, Math.floor(dailyDone / dailyTarget * 100)) : 0;
  document.getElementById('peCompletionPercent').innerText = `${percent}%`;
  document.getElementById('peCompletionDetail').innerText = dailyTarget
    ? `${dailyDone} / ${dailyTarget} profiles engaged · engagement completion`
    : 'Engagement completion · awaiting daily target';
  const phaseCount = Math.max(1, Math.min(6, Number(config.engagement_batch_count) || 3));
  const phases = batches.length ? batches : Array.from({length: phaseCount}, (_, i) => ({number: i + 1, target: 0, engaged: 0}));
  const progress = document.getElementById('pePhaseProgress');
  progress.setAttribute('aria-valuenow', String(percent));
  progress.innerHTML = phases.map(batch => {
    const fill = batch.target > 0 ? Math.min(100, Math.max(0, Number(batch.engaged || 0) / batch.target * 100)) : 0;
    return `<div class="pe-phase-track" style="flex:${Math.max(1, Number(batch.target) || 1)}"><div style="width:${fill}%"></div></div>`;
  }).join('');
  document.getElementById('pePhaseLabels').innerHTML = phases.map(batch => `<div style="flex:${Math.max(1, Number(batch.target) || 1)}"><strong>Phase ${escapeHtmlSafe(String(batch.number))}</strong><span>${Number(batch.engaged) || 0} / ${Number(batch.target) || '—'} · ${escapeHtmlSafe(titleCaseStatusClient(batch.status || 'pending'))}</span></div>`).join('');
  const currentBatchNumber = Number(campaign.current_batch_number || 0);
  const activeBatch = batches.find(batch => Number(batch.number) === currentBatchNumber) || batches.find(batch => batch.status !== 'completed');
  document.getElementById('peBatchValue').innerText = activeBatch ? `Batch ${activeBatch.number} / ${batches.length}` : (batches.length ? `${batches.length} complete` : '—');
  const nextBatchAt = campaign.next_batch_at ? new Date(campaign.next_batch_at) : null;
  document.getElementById('peBatchDetail').innerText = status === 'waiting_next_batch' && nextBatchAt && !Number.isNaN(nextBatchAt.valueOf())
    ? `next window ${nextBatchAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    : activeBatch ? `${activeBatch.engaged || 0} / ${activeBatch.target || 0} engagements in this window` : 'three paced windows per live campaign';
  document.getElementById('peConnectionsValue').innerText = `${campaign.connections_sent || 0} / ${campaign.connection_target ?? config.connection_target ?? 10}`;
  document.getElementById('peCandidatesValue').innerText = String(candidates.length);
  const bestCoverage = sources.reduce((best, source) => Math.max(best, Number(source.coverage || 0)), 0);
  document.getElementById('peCoverageDetail').innerText = sources.length ? `${Math.round(bestCoverage * 100)}% best source extraction coverage` : 'no source extracted';
  document.getElementById('peHighSignalValue').innerText = `${campaign.followed || 0} / ${campaign.follow_target ?? config.follow_target ?? 10}`;
  const pauseButton = document.getElementById('pePauseBtn');
  const resumeButton = document.getElementById('peResumeBtn');
  if (pauseButton) pauseButton.disabled = !data.is_running;
  if (resumeButton) {
    resumeButton.disabled = data.is_running || !['paused', 'pause_requested', 'failed'].includes(status);
    resumeButton.innerText = status === 'failed' ? 'Retry from checkpoint' : 'Resume';
  }

  document.getElementById('peSourceList').innerHTML = sources.length ? sources.map((source, index) => {
    let url = '';
    try {
      const parsed = new URL(source.resolved_url || source.submitted_url);
      if (parsed.protocol === 'https:' && /(^|\.)(linkedin\.com|lnkd\.in)$/i.test(parsed.hostname)) url = parsed.href;
    } catch (_) {}
    return `<div class="pe-source-row"><div class="pe-source-identity"><span class="pe-source-icon" aria-hidden="true">in</span><div><strong>Source post ${index + 1}</strong>${url ? `<a href="${escapeHtmlSafe(url)}" target="_blank" rel="noopener noreferrer">Open LinkedIn post ↗</a>` : '<small>Post link unavailable</small>'}</div></div><div class="pe-source-count"><strong>${Number(source.profiles_collected) || 0}${source.reaction_count ? ` / ${Number(source.reaction_count) || 0}` : ''}</strong><small>profiles collected</small></div><span class="obf-status-pill ${source.status === 'collected' ? 'success' : source.status === 'failed' ? 'warning' : 'neutral'}">${escapeHtmlSafe(titleCaseStatusClient(source.status))}</span></div>`;
  }).join('') : '<div class="empty-state">No source posts added today.</div>';

  document.getElementById('peAuditBody').innerHTML = candidates.length ? candidates.map(candidate => `<tr><td>${peProfileLink(candidate)}</td><td>${escapeHtmlSafe(candidate.location || '—')}</td><td>${candidate.follower_count ?? '—'}</td><td>${(candidate.posts || []).length}</td><td>${escapeHtmlSafe(candidate.very_active ? 'Very active' : candidate.activity_counts ? 'Below threshold' : 'Not measured')}</td><td>${escapeHtmlSafe(titleCaseStatusClient(candidate.status))}</td></tr>`).join('') : peEmptyRow(6, 'Candidates appear after source extraction.');
  const engaged = candidates.filter(candidate => candidate.likes_assigned || candidate.likes_completed || candidate.likes_preview);
  document.getElementById('peEngagementBody').innerHTML = engaged.length ? engaged.map(candidate => { const diversion = candidate.obf_diversion ? `${titleCaseStatusClient(candidate.obf_diversion)}${candidate.obf_diversion_seconds ? ` · ${candidate.obf_diversion_seconds}s` : ''}` : 'Pending plan'; const diversionResult = candidate.diversion_result; const diversionLabel = diversionResult?.error ? `${diversion} · retry-safe` : diversion; return `<tr><td>${peProfileLink(candidate)}</td><td>${candidate.likes_assigned || 0}</td><td>${candidate.likes_completed || 0}${candidate.likes_preview ? ` <small>(${candidate.likes_preview} preview)</small>` : ''}</td><td>${candidate.newest_post_age_hours == null ? '—' : `${candidate.newest_post_age_hours}h`}</td><td>${escapeHtmlSafe(diversionLabel)}</td><td>${escapeHtmlSafe(titleCaseStatusClient(candidate.status))}</td></tr>`; }).join('') : peEmptyRow(6, 'No like assignments recorded yet.');
  const ranked = candidates.filter(candidate => candidate.recommendation || candidate.activity_counts || candidate.connection_result || candidate.follow_result || candidate.connection_preview || candidate.follow_preview);
  document.getElementById('peConnectionsBody').innerHTML = ranked.length ? ranked.map(candidate => { const counts = candidate.activity_counts || {}; const decision = candidate.follow_result || candidate.follow_preview || candidate.connection_result?.status || candidate.connection_preview?.status || candidate.recommendation?.action || (candidate.very_active ? 'Ranked candidate' : 'Not eligible'); return `<tr><td>${peProfileLink(candidate)}</td><td>Tier ${candidate.geography_tier || 4} · ${escapeHtmlSafe(candidate.region || 'Unknown')}</td><td>${counts.reactions || 0} likes · ${counts.comments || 0} comments · ${counts.posts || 0} posts</td><td>${candidate.follower_count ?? '—'}</td><td>${escapeHtmlSafe(titleCaseStatusClient(decision))}</td></tr>`; }).join('') : peEmptyRow(5, 'Recommendations are saved immediately after each successful engagement.');
  const history = data.history || [];
  document.getElementById('peHistoryBody').innerHTML = history.length ? history.map(row => `<tr><td>${escapeHtmlSafe(row.day)}</td><td>${escapeHtmlSafe(titleCaseStatusClient(row.status))}</td><td>${row.engaged || 0} / ${row.target || 0}</td><td>${row.connections_sent || 0}</td><td>${row.followed || 0}</td><td>${row.dry_run ? 'Audit' : 'Live'}</td></tr>`).join('') : peEmptyRow(6, 'No completed campaign runs yet.');

  document.getElementById('peCdpAccount').value = config.cdp_account || 'design';
  // The operational test discussed for the first source uses Automation;
  // once a source is added, the campaign's own persisted lane always wins.
  document.getElementById('peSourceCdpAccount').value = campaign.cdp_account || (sources.length ? config.cdp_account : 'automation') || 'automation';
  document.getElementById('peMinTarget').value = config.engagement_min ?? 45;
  document.getElementById('peMaxTarget').value = config.engagement_max ?? 60;
  document.getElementById('peConnectionTarget').value = config.connection_target ?? 10;
  document.getElementById('peFollowTarget').value = config.follow_target ?? 10;
  document.getElementById('peFollowerLimit').value = config.follower_connection_limit ?? 5000;
  document.getElementById('peCoverage').value = config.reactor_min_coverage ?? 0.9;
  document.getElementById('peBatchCount').value = config.engagement_batch_count ?? 3;
  document.getElementById('peBatchDelay').value = config.inter_batch_delay_minutes ?? 90;
}

async function refreshPostEngagementDashboard() {
  try {
    renderPostEngagement(await ipcRenderer.invoke('read-post-engagement-dashboard', {}));
  } catch (error) {
    appendToConsole(`>>> Post Engagement state failed to load: ${error.message}`, 'error');
  }
}

document.querySelectorAll('[data-pe-tab]').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('[data-pe-tab]').forEach(item => item.classList.toggle('active', item === tab));
  document.querySelectorAll('[data-pe-panel]').forEach(panel => panel.classList.toggle('active', panel.dataset.pePanel === tab.dataset.peTab));
}));

document.getElementById('peAddSourceBtn')?.addEventListener('click', async () => {
  const input = document.getElementById('peSourceUrl');
  const url = input.value.trim();
  if (!url) return;
  const cdpAccount = document.getElementById('peSourceCdpAccount').value;
  const result = await ipcRenderer.invoke('add-post-engagement-source', { url, cdp_account: cdpAccount });
  if (result?.ok === false) appendToConsole(`>>> Could not add source: ${result.error}`, 'error');
  else { input.value = ''; await refreshPostEngagementDashboard(); }
});

document.getElementById('peArchiveFreshBtn')?.addEventListener('click', async () => {
  if (!window.confirm('Archive this campaign and create a fresh run for today? Completed actions stay in the daily deduplication ledger.')) return;
  const result = await ipcRenderer.invoke('archive-post-engagement-and-fresh', {});
  appendToConsole(result?.ok ? '>>> Campaign archived. Fresh same-day engagement run created; final send capacity remains shared for the day.' : `>>> Could not start fresh: ${result?.error}`, result?.ok ? 'system' : 'error');
  await refreshPostEngagementDashboard();
});

document.getElementById('peSaveSettingsBtn')?.addEventListener('click', async () => {
  const payload = {
    cdp_account: document.getElementById('peCdpAccount').value,
    engagement_min: Number(document.getElementById('peMinTarget').value),
    engagement_max: Number(document.getElementById('peMaxTarget').value),
    connection_target: Number(document.getElementById('peConnectionTarget').value),
    follow_target: Number(document.getElementById('peFollowTarget').value),
    follower_connection_limit: Number(document.getElementById('peFollowerLimit').value),
    reactor_min_coverage: Number(document.getElementById('peCoverage').value),
    engagement_batch_count: Number(document.getElementById('peBatchCount').value),
    inter_batch_delay_minutes: Number(document.getElementById('peBatchDelay').value)
  };
  const result = await ipcRenderer.invoke('save-post-engagement-settings', payload);
  appendToConsole(result?.ok === false ? `>>> Settings failed: ${result.error}` : '>>> Post Engagement settings saved.', result?.ok === false ? 'error' : 'success');
  await refreshPostEngagementDashboard();
});

document.getElementById('peDryRunBtn')?.addEventListener('click', async () => {
  const result = await ipcRenderer.invoke('start-post-engagement-dry-run', {});
  appendToConsole(result?.ok ? '>>> Post Engagement audit started in its dedicated tab.' : `>>> Audit did not start: ${result?.error}`, result?.ok ? 'system' : 'error');
  await refreshPostEngagementDashboard();
});

document.getElementById('peRunBtn')?.addEventListener('click', async () => {
  if (!window.confirm('Launch real LinkedIn Likes, follows, and connection requests for this campaign?')) return;
  const result = await ipcRenderer.invoke('start-post-engagement-run', {});
  appendToConsole(result?.ok ? '>>> Live Post Engagement workflow started.' : `>>> Workflow did not start: ${result?.error}`, result?.ok ? 'system' : 'error');
  await refreshPostEngagementDashboard();
});

document.getElementById('pePauseBtn')?.addEventListener('click', async () => {
  const result = await ipcRenderer.invoke('pause-post-engagement-run', {});
  appendToConsole(result?.ok ? '>>> Safe pause requested. The current browser step will finish, then the campaign will pause.' : `>>> Pause failed: ${result?.error}`, result?.ok ? 'system' : 'error');
  await refreshPostEngagementDashboard();
});

document.getElementById('peResumeBtn')?.addEventListener('click', async () => {
  const result = await ipcRenderer.invoke('resume-post-engagement-run', {});
  appendToConsole(result?.ok ? '>>> Post Engagement resumed from its saved checkpoint.' : `>>> Resume failed: ${result?.error}`, result?.ok ? 'system' : 'error');
  await refreshPostEngagementDashboard();
});

ipcRenderer.on('post-engagement-updated', refreshPostEngagementDashboard);

launchChromeBtn.addEventListener('click', () => {
  appendToConsole('>>> Launching Chrome Automation Profile...', 'system');
  ipcRenderer.send('launch-chrome', 'cdp1');
});

const stopBtns = document.querySelectorAll('.btn-stop-run');
stopBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    ipcRenderer.send('stop-command');
    appendToConsole('>>> Sending termination signal...', 'error');
  });
});

const watcherBtn = document.getElementById('overviewWatcherBtn');
let isWatcherActive = false;

if (watcherBtn) {
  watcherBtn.addEventListener('click', async () => {
    const requestedState = !isWatcherActive;
    isWatcherActive = requestedState;
    updateWatcherUI();

    appendToConsole(`>>> ${requestedState ? 'Enabling' : 'Disabling'} background auto-watcher...`, 'system');
    try {
      const result = await ipcRenderer.invoke('toggle-watcher', requestedState);
      if (result === false || result?.ok === false) throw new Error(result?.error || 'Watcher state change failed.');
    } catch (error) {
      appendToConsole(`>>> Watcher state change failed: ${error.message}`, 'error');
    } finally {
      await syncWatcherState();
    }
  });
}

function updateWatcherUI() {
  if (!watcherBtn) return;
  if (isWatcherActive) {
    watcherBtn.classList.add('active');
    watcherBtn.innerText = 'Watcher active';
  } else {
    watcherBtn.classList.remove('active');
    watcherBtn.innerText = 'Enable watcher';
  }
}

async function syncWatcherState() {
  isWatcherActive = await ipcRenderer.invoke('check-watcher-status');
  updateWatcherUI();
}

// Initialization
window.addEventListener('DOMContentLoaded', () => {
  let savedPage = 'overview';
  try {
    const stored = localStorage.getItem(selectedPageStorageKey);
    if (Object.prototype.hasOwnProperty.call(pageMetadata, stored)) savedPage = stored;
  } catch (_) {}
  showPage(savedPage);
  if (todayDate) {
    todayDate.innerText = formatHeaderDate();
  }
  refreshStats();
  refreshObfDashboard();
  refreshLeadPrepDashboard();
  refreshJobDiscoveryDashboard();
  refreshPostEngagementDashboard();
  syncWatcherState();
  setInterval(syncWatcherState, 5000); // Check watcher status every 5s
  setInterval(() => {
    if (appContainer.classList.contains('open')) {
      refreshStats();
      const activePage = document.querySelector('.page-view.active');
      if (activePage?.dataset.page === 'obf') refreshObfDashboard();
      if (activePage?.dataset.page === 'lead-prep') refreshLeadPrepDashboard();
      if (activePage?.dataset.page === 'job-discovery') refreshJobDiscoveryDashboard();
      if (activePage?.dataset.page === 'post-engagement') refreshPostEngagementDashboard();
    }
  }, 5000);
});
