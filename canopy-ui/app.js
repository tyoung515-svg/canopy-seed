const API_BASE = `${window.location.protocol}//${window.location.host}`;
const FRIENDLY_OFFLINE = "Canopy isn't running yet. Start it with python start.py and refresh.";

const state = {
  currentView: 'welcome',
  sessionId: sessionStorage.getItem('canopy_session_id') || null,
  model: localStorage.getItem('canopy_model') || 'claude',
  waiting: false,
  pendingImageData: null,
  context: {
    name: '',
    goal: '',
    users: '',
    features: [],
    architecture: '',
    description: '',
    research: [],
    open_questions: []
  },
  eventSource: null,
  typingMessageId: null,
  activeResearchIndicator: null,
  loadedFirstContext: false,
  snapshotCount: 1
};

const el = {};

document.addEventListener('DOMContentLoaded', init);

function init() {
  cacheElements();
  bindEvents();
  applyModelSelection();
  connectSSE();
  checkHealth();

  if (state.sessionId) {
    transitionTo('conversation');
    hydrateContext().catch(() => {
      addSystemMessage(FRIENDLY_OFFLINE);
    });
  }
}

function cacheElements() {
  el.healthBanner = document.getElementById('health-banner');
  el.stateWelcome = document.getElementById('state-welcome');
  el.stateConversation = document.getElementById('state-conversation');
  el.stateBuilding = document.getElementById('state-building');

  el.modelInputs = document.querySelectorAll('input[name="model"]');
  el.plantSeedBtn = document.getElementById('plant-seed-btn');
  el.loadContextPath = document.getElementById('load-context-path');
  el.loadContextBtn = document.getElementById('load-context-btn');
  el.loadContextStatus = document.getElementById('load-context-status');

  el.chatMessages = document.getElementById('chat-messages');
  el.chatInput = document.getElementById('chat-input');
  el.sendBtn = document.getElementById('send-btn');
  el.dropZone = document.getElementById('image-drop-zone');
  el.imageInput = document.getElementById('image-input');
  el.imagePreview = document.getElementById('image-preview');
  el.researchHeaderIndicator = document.getElementById('header-research-indicator');

  el.contextFields = document.getElementById('context-fields');
  el.sidebarSkeleton = document.getElementById('sidebar-skeleton');
  el.researchList = document.getElementById('research-list');
  el.openQuestions = document.getElementById('open-questions');

  el.summaryName = document.getElementById('summary-name');
  el.summaryDescription = document.getElementById('summary-description');
  el.summaryFeatures = document.getElementById('summary-features');
  el.summaryUsers = document.getElementById('summary-users');
  el.summaryArchitecture = document.getElementById('summary-architecture');
  el.summaryResearch = document.getElementById('summary-research');
  el.snapshotBar = document.getElementById('snapshot-bar');

  el.startBuildingBtn = document.getElementById('start-building-btn');
  el.exportOverviewBtn = document.getElementById('export-overview-btn');
  el.goBackBtn = document.getElementById('go-back-btn');

  el.messageTemplate = document.getElementById('chat-message-template');
}

function bindEvents() {
  el.modelInputs.forEach((input) => {
    input.addEventListener('change', () => {
      state.model = input.value;
      localStorage.setItem('canopy_model', state.model);
    });
  });

  el.plantSeedBtn.addEventListener('click', onPlantSeed);
  if (el.loadContextBtn) {
    el.loadContextBtn.addEventListener('click', onLoadContextBuild);
  }
  el.sendBtn.addEventListener('click', onSendMessage);

  el.chatInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      onSendMessage();
    }
  });

  el.dropZone.addEventListener('click', () => el.imageInput.click());
  el.dropZone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      el.imageInput.click();
    }
  });

  el.imageInput.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (file) {
      await attachImage(file);
      event.target.value = '';
    }
  });

  el.dropZone.addEventListener('dragover', (event) => {
    event.preventDefault();
    el.dropZone.classList.add('drag-over');
  });

  el.dropZone.addEventListener('dragleave', () => {
    el.dropZone.classList.remove('drag-over');
  });

  el.dropZone.addEventListener('drop', async (event) => {
    event.preventDefault();
    el.dropZone.classList.remove('drag-over');
    const file = event.dataTransfer?.files?.[0];
    if (file && file.type.startsWith('image/')) {
      await attachImage(file);
    }
  });

  el.startBuildingBtn.addEventListener('click', onStartBuilding);
  el.exportOverviewBtn.addEventListener('click', onExportOverview);
  el.goBackBtn.addEventListener('click', () => transitionTo('conversation'));
}

function applyModelSelection() {
  el.modelInputs.forEach((input) => {
    input.checked = input.value === state.model;
  });
}

async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/api/health`);
    if (!res.ok) {
      throw new Error('Health check failed');
    }
    el.healthBanner.classList.add('hidden');
  } catch {
    el.healthBanner.classList.remove('hidden');
  }
}

async function onPlantSeed() {
  el.plantSeedBtn.disabled = true;
  try {
    const data = await startSession(state.model);
    state.sessionId = data.session_id;
    sessionStorage.setItem('canopy_session_id', state.sessionId);

    transitionTo('conversation');

    const opening = data.opening_message || '🌱 What would you like to build today? Start with what problem you are trying to solve.';
    addChatMessage('canopy', opening);
    hideSidebarSkeletonIfReady(true);
  } catch {
    showFriendlyOfflineMessage();
  } finally {
    el.plantSeedBtn.disabled = false;
  }
}

function onLoadContextBuild() {
  const path = (el.loadContextPath?.value || '').trim();
  const status = el.loadContextStatus;
  if (!status) return;

  if (!path) {
    status.textContent = 'Enter a path first.';
    return;
  }

  status.style.color = '#94a3b8';
  status.textContent = 'Loading…';

  fetch(`${API_BASE}/api/canopy/session/load`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ context_path: path })
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.error) {
        status.style.color = '#f87171';
        status.textContent = data.error;
        return;
      }
      window.location.href = data.devhub_url;
    })
    .catch(() => {
      status.style.color = '#f87171';
      status.textContent = 'Request failed.';
    });
}

async function onSendMessage() {
  const text = el.chatInput.value.trim();
  if (!text && !state.pendingImageData) {
    return;
  }
  if (!state.sessionId || state.waiting) {
    return;
  }

  const imageData = state.pendingImageData;
  const imagePreviewSrc = imageData;

  addChatMessage('user', text || '[Image uploaded]', imagePreviewSrc || null);

  el.chatInput.value = '';
  clearPendingImage();

  setWaiting(true);
  const typingMessage = addTypingIndicator();

  try {
    const data = await sendMessage(state.sessionId, text, imageData);
    removeTypingIndicator(typingMessage);

    if (data.reply) {
      addChatMessage('canopy', data.reply);
    }

    if (data.context_delta) {
      updateContextSidebar(data.context_delta);
      mergeContext(data.context_delta);
    }

    if (data.researching && typeof data.researching === 'string') {
      showResearchIndicator(data.researching);
    }

    if (data.ready) {
      const context = await getContext(state.sessionId).catch(() => ({ context: state.context }));
      const finalContext = context.context || context || state.context;
      transitionToBuilding(finalContext);
    }
  } catch {
    removeTypingIndicator(typingMessage);
    addSystemMessage(FRIENDLY_OFFLINE);
  } finally {
    setWaiting(false);
  }
}

function setWaiting(waiting) {
  state.waiting = waiting;
  el.sendBtn.disabled = waiting;
  el.chatInput.disabled = waiting;
}

function addTypingIndicator() {
  const message = document.createElement('article');
  message.className = 'chat-message';
  message.innerHTML = `
    <div class="bubble">
      🌱 <span class="typing"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span>
    </div>
  `;
  el.chatMessages.appendChild(message);
  autoScrollChat();
  return message;
}

function removeTypingIndicator(node) {
  if (node && node.parentNode) {
    node.remove();
  }
}

function addSystemMessage(text) {
  const msg = addChatMessage('canopy', text);
  msg.querySelector('.bubble').style.borderColor = '#D4845A';
}

function addChatMessage(role, text, imageSrc = null) {
  const fragment = el.messageTemplate.content.cloneNode(true);
  const node = fragment.querySelector('.chat-message');
  const bubble = fragment.querySelector('.bubble');

  node.classList.add(role === 'user' ? 'user' : 'canopy');
  bubble.textContent = role === 'canopy' ? `🌱 ${text}` : text;

  if (imageSrc) {
    const image = document.createElement('img');
    image.src = imageSrc;
    image.alt = 'Uploaded reference';
    image.className = 'message-image';
    bubble.appendChild(image);
  }

  el.chatMessages.appendChild(fragment);
  autoScrollChat();
  return el.chatMessages.lastElementChild;
}

function autoScrollChat() {
  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
}

async function attachImage(file) {
  const dataUrl = await readFileAsDataURL(file);
  state.pendingImageData = dataUrl;

  el.imagePreview.innerHTML = `<img src="${dataUrl}" alt="Image preview" /><span>${escapeHtml(file.name)}</span>`;
  el.imagePreview.classList.remove('hidden');
}

function clearPendingImage() {
  state.pendingImageData = null;
  el.imagePreview.innerHTML = '';
  el.imagePreview.classList.add('hidden');
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function connectSSE() {
  if (state.eventSource) {
    state.eventSource.close();
  }

  const es = new EventSource(`${API_BASE}/api/devhub/events`);
  state.eventSource = es;

  es.addEventListener('canopy_context_update', (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.context_delta) {
        updateContextSidebar(data.context_delta);
        mergeContext(data.context_delta);
      }
    } catch {
      // noop
    }
  });

  es.addEventListener('canopy_research_start', (event) => {
    try {
      const data = JSON.parse(event.data);
      showResearchIndicator(data.query || 'gathering details');
    } catch {
      // noop
    }
  });

  es.addEventListener('canopy_research_complete', (event) => {
    try {
      const data = JSON.parse(event.data);
      hideResearchIndicator();
      if (data.entry) {
        addResearchCitation(data.entry);
      }
    } catch {
      // noop
    }
  });

  es.addEventListener('canopy_context_ready', (event) => {
    try {
      const data = JSON.parse(event.data);
      transitionToBuilding(data.context || state.context);
    } catch {
      transitionToBuilding(state.context);
    }
  });

  es.addEventListener('canopy_snapshot_created', (event) => {
    try {
      const data = JSON.parse(event.data);
      updateSnapshotBar(data.snapshot);
    } catch {
      // noop
    }
  });

  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

function showResearchIndicator(queryText) {
  const text = `🔍 Researching: "${queryText}"`;
  el.researchHeaderIndicator.textContent = text;
  el.researchHeaderIndicator.classList.remove('hidden');

  if (state.activeResearchIndicator?.parentNode) {
    state.activeResearchIndicator.remove();
  }

  const indicator = document.createElement('div');
  indicator.className = 'research-indicator';
  indicator.textContent = text;
  el.chatMessages.appendChild(indicator);
  state.activeResearchIndicator = indicator;
  autoScrollChat();
}

function hideResearchIndicator() {
  el.researchHeaderIndicator.classList.add('hidden');
  if (state.activeResearchIndicator?.parentNode) {
    state.activeResearchIndicator.remove();
    state.activeResearchIndicator = null;
  }
}

function updateContextSidebar(delta) {
  const mappings = {
    name: ['name', 'project_name', 'title'],
    goal: ['goal', 'problem', 'objective', 'description', 'conversation_summary'],
    users: ['users', 'target_users', 'audience'],
    features: ['features', 'key_features', 'goals']   // goals is the ProjectContext field name
  };

  Object.entries(mappings).forEach(([field, aliases]) => {
    const key = aliases.find((k) => Object.prototype.hasOwnProperty.call(delta, k));
    if (!key) return;

    const value = normalizeValue(delta[key]);
    const row = el.contextFields.querySelector(`[data-context-key="${field}"] strong`);
    if (row) {
      row.textContent = value || '—';
      const fieldRow = row.closest('.context-field');
      fieldRow.classList.remove('flash');
      void fieldRow.offsetWidth;
      fieldRow.classList.add('flash');
    }
  });

  const questionKeys = ['open_questions', 'questions', 'pending_questions'];
  const openKey = questionKeys.find((k) => Object.prototype.hasOwnProperty.call(delta, k));
  if (openKey) {
    renderOpenQuestions(delta[openKey]);
  }

  hideSidebarSkeletonIfReady();
}

function mergeContext(delta) {
  state.context = {
    ...state.context,
    ...delta
  };

  if (Array.isArray(delta.research)) {
    state.context.research = delta.research;
    renderResearchList(state.context.research);
  }

  if (Array.isArray(delta.open_questions)) {
    state.context.open_questions = delta.open_questions;
    renderOpenQuestions(state.context.open_questions);
  }
}

function renderResearchList(researchEntries = []) {
  el.researchList.innerHTML = '';
  if (!researchEntries.length) {
    el.researchList.innerHTML = '<p class="empty-text">Research entries appear here as they\'re found.</p>';
    return;
  }

  researchEntries.forEach((entry) => {
    addResearchCitation(entry);
  });
}

function addResearchCitation(entry) {
  const empty = el.researchList.querySelector('.empty-text');
  if (empty) empty.remove();

  const chip = document.createElement('span');
  chip.className = 'chip';

  if (typeof entry === 'string') {
    chip.textContent = entry;
  } else {
    const title = entry.title || entry.source || entry.url || 'Source';
    chip.textContent = title;
  }

  el.researchList.appendChild(chip);
}

function renderOpenQuestions(questions = []) {
  const list = Array.isArray(questions) ? questions : [questions];
  el.openQuestions.innerHTML = '';
  if (!list.length || !String(list[0]).trim()) {
    el.openQuestions.innerHTML = '<li class="empty-text">Questions Big Brain still needs answered.</li>';
    return;
  }

  list.forEach((question) => {
    const li = document.createElement('li');
    li.textContent = String(question);
    el.openQuestions.appendChild(li);
  });
}

function hideSidebarSkeletonIfReady(force = false) {
  if (force || !state.loadedFirstContext) {
    state.loadedFirstContext = true;
    el.sidebarSkeleton.classList.add('hidden');
  }
}

function transitionTo(view) {
  const views = {
    welcome: el.stateWelcome,
    conversation: el.stateConversation,
    building: el.stateBuilding
  };

  if (state.currentView === view) {
    return;
  }

  const previous = views[state.currentView];
  const next = views[view];

  if (!next) return;

  if (previous) {
    previous.classList.remove('state-active');
    setTimeout(() => {
      previous.hidden = true;
    }, 300);
  }

  next.hidden = false;
  requestAnimationFrame(() => {
    next.classList.add('state-active');
  });

  state.currentView = view;
}

function transitionToBuilding(context = {}) {
  mergeContext(context);
  fillBuildingSummary();
  transitionTo('building');
  fetchSnapshots().catch(() => {
    updateSnapshotBar();
  });
}

function fillBuildingSummary() {
  const projectName = firstTruthy(state.context.name, state.context.project_name, '—');
  const description = firstTruthy(
    state.context.description,
    state.context.conversation_summary,
    state.context.goal,
    'Your project summary will appear here.'
  );

  // Goals/features — context uses 'goals' (array), fallback to 'features'
  const goalsArr = Array.isArray(state.context.goals) ? state.context.goals
    : Array.isArray(state.context.features) ? state.context.features : [];
  const features = goalsArr.length
    ? goalsArr.join(' · ')
    : normalizeValue(state.context.features || state.context.goals) || '—';

  const users = firstTruthy(state.context.target_users, state.context.users, state.context.audience, '—');

  // Architecture — context uses 'architecture_notes' (array)
  const archArr = Array.isArray(state.context.architecture_notes) ? state.context.architecture_notes
    : Array.isArray(state.context.tech_preferences) ? state.context.tech_preferences : [];
  const architecture = archArr.length
    ? archArr.join(' · ')
    : firstTruthy(state.context.architecture, state.context.notes, '—');

  const researchCount = Array.isArray(state.context.research_log)
    ? state.context.research_log.length
    : Array.isArray(state.context.research) ? state.context.research.length : 0;

  el.summaryName.textContent = `PROJECT: ${projectName}`;
  el.summaryDescription.textContent = description;
  el.summaryFeatures.textContent = features;
  el.summaryUsers.textContent = users;
  el.summaryArchitecture.textContent = architecture;
  el.summaryResearch.textContent = `${researchCount} source${researchCount === 1 ? '' : 's'}`;
}

async function onStartBuilding() {
  if (!state.sessionId) {
    addSystemMessage('No active session yet. Start from the welcome screen.');
    return;
  }

  el.startBuildingBtn.disabled = true;
  el.startBuildingBtn.textContent = '⏳ Saving spec…';

  try {
    // Export the project spec to disk first
    const exportData = await exportOverview(state.sessionId).catch(() => null);

    const exportedPath = exportData?.markdown_path || exportData?.json_path || '';

    // Navigate to DevHub with session context
    const devhubUrl = `${API_BASE}/devhub?session_id=${encodeURIComponent(state.sessionId)}&export_path=${encodeURIComponent(exportedPath)}`;
    window.location.href = devhubUrl;

  } catch (err) {
    console.error('Start Building error:', err);
    el.startBuildingBtn.disabled = false;
    el.startBuildingBtn.textContent = '🌿 Start Building';
    addSystemMessage('Could not save spec before launching DevHub. Check the server logs.');
  }
}

async function onExportOverview() {
  if (!state.sessionId) {
    addSystemMessage('No active session yet. Start from the welcome screen.');
    return;
  }

  el.exportOverviewBtn.disabled = true;
  try {
    const data = await exportOverview(state.sessionId).catch(() => null);

    const projectName = firstTruthy(state.context.name, state.context.project_name, 'Project');
    const slug = projectName.replace(/[^a-zA-Z0-9]/g, '_').replace(/_+/g, '_');

    const markdown = buildOverviewMarkdown();
    const json = JSON.stringify(state.context, null, 2);

    downloadText(markdown, `${slug}_PROJECT_OVERVIEW.md`, 'text/markdown');
    downloadText(json, `${slug}_PROJECT_CONTEXT.json`, 'application/json');

    const savedPath = data?.markdown_path
      ? data.markdown_path
      : 'exports/ folder in your Canopy Seed directory';
    addSystemMessage(`✅ Exported "${projectName}". Files saved to your Downloads and to: ${savedPath}`);
  } catch {
    // Still try the download even if the API call failed
    const markdown = buildOverviewMarkdown();
    downloadText(markdown, 'PROJECT_OVERVIEW.md', 'text/markdown');
    addSystemMessage('Export downloaded. (Server save may have failed — check exports/ folder.)');
  } finally {
    el.exportOverviewBtn.disabled = false;
  }
}

function buildOverviewMarkdown() {
  const name = firstTruthy(state.context.name, state.context.project_name, 'Untitled Project');
  const goal = firstTruthy(state.context.goal, state.context.description, 'No summary provided.');
  const features = Array.isArray(state.context.features) ? state.context.features : splitMaybeList(state.context.features);
  const users = firstTruthy(state.context.users, state.context.target_users, 'Not specified');
  const architecture = firstTruthy(state.context.architecture, state.context.notes, 'Not specified');

  return [
    `# ${name}`,
    '',
    goal,
    '',
    '## Key Features',
    ...(features.length ? features.map((feature) => `- ${feature}`) : ['- Not specified']),
    '',
    `## Target Users\n${users}`,
    '',
    `## Architecture\n${architecture}`,
    '',
    `## Research Used\n${(state.context.research || []).length} sources`
  ].join('\n');
}

function downloadText(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function hydrateContext() {
  if (!state.sessionId) return;
  const data = await getContext(state.sessionId);
  const context = data.context || data || {};
  mergeContext(context);
  updateContextSidebar(context);
  renderResearchList(state.context.research || []);
  renderOpenQuestions(state.context.open_questions || []);
  fillBuildingSummary();
  fetchSnapshots().catch(() => {
    updateSnapshotBar();
  });
}

async function fetchSnapshots() {
  const res = await fetch(`${API_BASE}/api/canopy/snapshots?session_id=${encodeURIComponent(state.sessionId || '')}`);
  if (!res.ok) {
    throw new Error('Snapshot fetch failed');
  }
  const data = await res.json();
  const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
  state.snapshotCount = Math.max(1, snapshots.length || data.count || 1);
  updateSnapshotBar();
}

function updateSnapshotBar(snapshot = null) {
  if (snapshot) {
    const idx = Number(snapshot.index || snapshot.version || state.snapshotCount + 1);
    state.snapshotCount = Number.isFinite(idx) ? idx : state.snapshotCount;
  }
  el.snapshotBar.textContent = `🕐 Version ${state.snapshotCount} of 3 slots available`;
}

function normalizeValue(value) {
  if (Array.isArray(value)) {
    return value.filter(Boolean).join(', ');
  }
  if (value && typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value || '').trim();
}

function firstTruthy(...values) {
  return values.find((value) => value !== null && value !== undefined && String(value).trim() !== '') || '';
}

function splitMaybeList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  return String(value)
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function showFriendlyOfflineMessage() {
  addSystemMessage(FRIENDLY_OFFLINE);
  el.healthBanner.classList.remove('hidden');
}

async function safeFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  return res;
}

async function startSession(model) {
  const res = await safeFetch(`${API_BASE}/api/canopy/session/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model })
  });
  return res.json();
}

async function sendMessage(session_id, text, image_data = null) {
  const res = await safeFetch(`${API_BASE}/api/canopy/session/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id, text, image_data })
  });
  return res.json();
}

async function getContext(session_id) {
  const res = await safeFetch(`${API_BASE}/api/canopy/session/context?session_id=${encodeURIComponent(session_id)}`);
  return res.json();
}

async function exportOverview(session_id) {
  const res = await safeFetch(`${API_BASE}/api/canopy/session/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id })
  });
  return res.json();
}

async function dispatchToDevHub(context) {
  const payload = {
    task: typeof context === 'string' ? context : JSON.stringify(context, null, 2)
  };
  const res = await safeFetch(`${API_BASE}/api/devhub/dispatch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  return res.json();
}

/* ============================================
   CS3 — Research Panel, Export, Snapshots
   ============================================ */

// Research Store and Functions
const cs3ResearchStore = {};

function cs3InitResearchPanel() {
  // Initialize research store from context
  if (Array.isArray(state.context.research)) {
    state.context.research.forEach((entry) => {
      const id = entry.query || entry.title || entry.url;
      if (id) cs3ResearchStore[id] = entry;
    });
  }
}

function cs3AddResearchEntry(entry) {
  if (!entry) return;
  const entryId = entry.query || entry.title || entry.url;
  if (!entryId) return;
  cs3ResearchStore[entryId] = entry;
  cs3RenderResearchChip(entry);
}

function cs3RenderResearchChip(entry) {
  const entryId = entry.query || entry.title || entry.url;
  const query = entry.query || entry.title || 'Research';
  const truncated = query.length > 40 ? query.substring(0, 37) + '...' : query;

  const chip = document.createElement('div');
  chip.className = 'cs3-research-chip';
  chip.setAttribute('data-entry-id', entryId);
  chip.innerHTML = `
    <span class="cs3-research-icon">🔍</span>
    <span class="cs3-research-query">${escapeHtml(truncated)}</span>
    <span class="cs3-research-expand">↗</span>
  `;
  chip.addEventListener('click', () => cs3OpenResearch(entryId));

  const list = document.getElementById('research-list');
  const empty = list?.querySelector('.empty-text');
  if (empty) empty.remove();
  list?.appendChild(chip);
}

function cs3OpenResearch(entryId) {
  const entry = cs3ResearchStore[entryId];
  if (!entry) return;

  const modal = document.getElementById('cs3-research-modal');
  const queryEl = document.getElementById('cs3-modal-query');
  const summaryEl = document.getElementById('cs3-modal-summary');
  const citationsEl = document.getElementById('cs3-modal-citations-list');

  queryEl.textContent = entry.query || entry.title || 'Research Entry';
  summaryEl.textContent = entry.summary || entry.description || entry.content || 'No summary available.';

  citationsEl.innerHTML = '';
  if (Array.isArray(entry.citations) && entry.citations.length > 0) {
    entry.citations.forEach((citation) => {
      const li = document.createElement('li');
      if (typeof citation === 'string') {
        li.textContent = citation;
      } else {
        const title = citation.title || citation.url || 'Source';
        const url = citation.url;
        if (url) {
          li.innerHTML = `<a href="${escapeHtml(url)}" target="_blank">${escapeHtml(title)}</a>`;
        } else {
          li.textContent = title;
        }
      }
      citationsEl.appendChild(li);
    });
  } else if (entry.url) {
    const li = document.createElement('li');
    li.innerHTML = `<a href="${escapeHtml(entry.url)}" target="_blank">${escapeHtml(entry.url)}</a>`;
    citationsEl.appendChild(li);
  } else {
    const li = document.createElement('li');
    li.textContent = 'No sources listed.';
    citationsEl.appendChild(li);
  }

  modal.classList.remove('hidden');
}

function cs3CloseResearch() {
  const modal = document.getElementById('cs3-research-modal');
  modal.classList.add('hidden');
}

// Export Functions
let cs3ExportedPaths = { markdown: '', json: '' };

function cs3InitExportPanel() {
  // Wire up export button override if needed
}

async function cs3TriggerExport(sessionId) {
  const exportBtn = document.getElementById('export-overview-btn');
  if (!exportBtn) return;

  const originalText = exportBtn.textContent;
  exportBtn.textContent = 'Exporting...';
  exportBtn.disabled = true;

  try {
    const result = await fetch(`${API_BASE}/api/canopy/session/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    }).then(r => r.json());

    cs3ExportedPaths.markdown = result.markdown_path || 'PROJECT_OVERVIEW.md';
    cs3ExportedPaths.json = result.json_path || 'PROJECT_CONTEXT.json';

    cs3ShowExportModal();
  } catch (err) {
    console.error('Export failed:', err);
    showToast('Export failed. Please try again.', 'error');
  } finally {
    exportBtn.textContent = originalText;
    exportBtn.disabled = false;
  }
}

function cs3ShowExportModal() {
  const modal = document.getElementById('cs3-export-modal');
  const pathsEl = document.getElementById('cs3-export-paths');

  if (pathsEl) {
    pathsEl.innerHTML = `
      <code>${escapeHtml(cs3ExportedPaths.markdown)}</code><br/>
      <code>${escapeHtml(cs3ExportedPaths.json)}</code>
    `;
  }

  modal.classList.remove('hidden');
}

function cs3CloseExport() {
  const modal = document.getElementById('cs3-export-modal');
  modal.classList.add('hidden');
}

function cs3CopyExportPath() {
  const paths = `${cs3ExportedPaths.markdown}\n${cs3ExportedPaths.json}`;
  navigator.clipboard.writeText(paths).then(() => {
    showToast('Paths copied to clipboard', 'success');
  }).catch(() => {
    showToast('Failed to copy paths', 'error');
  });
}

// Snapshot Functions
const cs3SnapshotStore = {};

function cs3InitSnapshotPanel() {
  const panel = document.getElementById('cs3-snapshot-panel');
  if (panel && state.currentView === 'building') {
    cs3LoadSnapshots();
  }
}

async function cs3LoadSnapshots() {
  try {
    const res = await fetch(`${API_BASE}/api/canopy/snapshots?session_id=${encodeURIComponent(state.sessionId || '')}`);
    if (!res.ok) throw new Error('Snapshot fetch failed');

    const data = await res.json();
    const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];

    cs3RenderSnapshotList(snapshots);
  } catch (err) {
    console.error('Failed to load snapshots:', err);
    cs3RenderSnapshotList([]);
  }
}

function cs3RenderSnapshotList(snapshots) {
  const panel = document.getElementById('cs3-snapshot-panel');
  const list = document.getElementById('cs3-snapshot-list');
  const empty = document.getElementById('cs3-snapshot-empty');
  const count = document.getElementById('cs3-snapshot-count');

  if (!snapshots || snapshots.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    if (count) count.textContent = '0 of 3 snapshots';
    return;
  }

  empty.classList.add('hidden');
  list.innerHTML = '';
  cs3SnapshotStore = {};

  snapshots.forEach((snapshot) => {
    const id = snapshot.id || snapshot.snapshot_id;
    if (id) cs3SnapshotStore[id] = snapshot;
    cs3RenderSnapshotItem(snapshot, list);
  });

  if (count) count.textContent = `${snapshots.length} of 3 snapshots`;
}

function cs3RenderSnapshotItem(snapshot, container) {
  const id = snapshot.id || snapshot.snapshot_id;
  const description = snapshot.description || snapshot.label || `Version ${snapshot.version || 'unknown'}`;
  const timestamp = snapshot.timestamp || snapshot.created_at;
  const formatted = timestamp ? new Date(timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'Unknown time';

  const item = document.createElement('div');
  item.className = 'cs3-snapshot-item';
  item.setAttribute('data-snapshot-id', id);
  item.innerHTML = `
    <div class="cs3-snapshot-info">
      <span class="cs3-snapshot-label">${escapeHtml(description)}</span>
      <span class="cs3-snapshot-time">${escapeHtml(formatted)}</span>
    </div>
    <button class="cs3-snapshot-rollback-btn" onclick="cs3RollbackTo('${escapeHtml(id)}')" title="Roll back to this version">
      ↩ Restore
    </button>
  `;

  container.appendChild(item);
}

async function cs3RollbackTo(snapshotId) {
  const snapshot = cs3SnapshotStore[snapshotId];
  if (!snapshot) return;

  const description = snapshot.description || snapshot.label || `Version ${snapshot.version || 'unknown'}`;

  const confirmed = confirm(
    `Restore to: "${description}"\n\n` +
    `This will undo all changes made after this snapshot.\n` +
    `Your current work will be backed up before restoring.\n\n` +
    `Continue?`
  );

  if (!confirmed) return;

  const btn = document.querySelector(`[data-snapshot-id="${snapshotId}"] .cs3-snapshot-rollback-btn`);
  const originalText = btn.textContent;
  btn.textContent = 'Restoring...';
  btn.disabled = true;

  try {
    const result = await fetch(`${API_BASE}/api/canopy/snapshots/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ snapshot_id: snapshotId })
    }).then(r => r.json());

    if (result.success) {
      showToast(`Restored to: ${description}`, 'success');
      await cs3LoadSnapshots();
    } else {
      showToast('Rollback failed. Please try again.', 'error');
    }
  } catch (err) {
    console.error('Rollback error:', err);
    showToast('Rollback failed. Please try again.', 'error');
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

function showToast(message, type = 'success') {
  const toast = document.createElement('div');
  toast.className = `cs3-toast cs3-toast-${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);

  setTimeout(() => toast.classList.add('cs3-toast-visible'), 10);
  setTimeout(() => {
    toast.classList.remove('cs3-toast-visible');
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

// Wire up CS3 components in DOMContentLoaded
const originalDOMContentLoaded = document.addEventListener.bind(document);

document.addEventListener('DOMContentLoaded', () => {
  cs3InitResearchPanel();
  cs3InitExportPanel();
  cs3InitSnapshotPanel();

  // Override export button click handler
  const exportBtn = document.querySelector('#export-overview-btn');
  if (exportBtn) {
    exportBtn.removeEventListener('click', onExportOverview);
    exportBtn.addEventListener('click', () => {
      if (!state.sessionId) {
        addSystemMessage('No active session yet. Start from the welcome screen.');
        return;
      }
      cs3TriggerExport(state.sessionId);
    });
  }

  // Show snapshot panel when in building state
  const snapshotPanel = document.getElementById('cs3-snapshot-panel');
  if (snapshotPanel && state.currentView === 'building') {
    snapshotPanel.hidden = false;
    cs3LoadSnapshots();
  }
}, { once: false });

// Hook into state transitions to show/hide snapshot panel
const originalTransitionToBuilding = window.transitionToBuilding;
if (originalTransitionToBuilding) {
  window.transitionToBuilding = function (context) {
    originalTransitionToBuilding.call(this, context);
    const snapshotPanel = document.getElementById('cs3-snapshot-panel');
    if (snapshotPanel) {
      snapshotPanel.hidden = false;
      cs3LoadSnapshots();
    }
  };
}

// Hook into SSE event for research complete
const originalConnectSSE = window.connectSSE;
if (originalConnectSSE) {
  window.connectSSE = function () {
    originalConnectSSE.call(this);

    const es = state.eventSource;
    if (es) {
      es.addEventListener('canopy_research_complete', (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.entry) {
            cs3AddResearchEntry(data.entry);
          }
        } catch {
          // noop
        }
      });
    }
  };
}
