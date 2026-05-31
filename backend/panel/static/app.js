const connection = document.querySelector("#connection");
const statusLine = document.querySelector("#statusLine");
const transcript = document.querySelector("#transcript");
const insightFeedViewport = document.querySelector("#insightFeedViewport");
const insightFeed = document.querySelector("#insightFeed");
const prepForm = document.querySelector("#prepForm");
const prepTopic = document.querySelector("#prepTopic");
const prepGoal = document.querySelector("#prepGoal");
const prepTerms = document.querySelector("#prepTerms");
const contextPill = document.querySelector("#contextPill");
const form = document.querySelector("#utteranceForm");
const input = document.querySelector("#utteranceInput");
const demoMode = document.querySelector("#demoMode");
const analysisProvider = document.querySelector("#analysisProvider");
const layoutArbiterToggle = document.querySelector("#layoutArbiterToggle");
const conversationPopup = document.querySelector("#conversationPopup");
const conversationText = document.querySelector("#conversationText");
const feedDetail = document.querySelector("#feedDetail");
const feedDetailClose = document.querySelector("#feedDetailClose");
const feedDetailKicker = document.querySelector("#feedDetailKicker");
const feedDetailTitle = document.querySelector("#feedDetailTitle");
const feedDetailBody = document.querySelector("#feedDetailBody");
const feedDetailPoints = document.querySelector("#feedDetailPoints");
const feedDetailAction = document.querySelector("#feedDetailAction");

let socket;
let micRunning = false;
let typingTimer;
let activeFeedDetailId = null;
let feedItemsById = new Map();

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    connection.textContent = "연결됨";
  });

  socket.addEventListener("close", () => {
    connection.textContent = "재연결 중";
    setTimeout(connect, 1200);
  });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "state") renderState(payload.state);
    if (payload.type === "transcript") addTranscript(payload.item);
    if (payload.type === "panel_update") updatePanel(payload.panel);
    if (payload.type === "feed_update") upsertFeedItem(payload.item);
    if (payload.type === "demo_utterance_start") typeConversation(payload.item);
    if (payload.type === "demo_utterance_end") finishConversation(payload.stopped);
    if (payload.type === "status") statusLine.textContent = payload.message;
  });
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify(message));
}

function renderState(state) {
  const provider = state.ai_provider === "exaone" ? "EXAONE API" : "CLI";
  statusLine.textContent = state.running ? `회의 진행 중 · ${provider}` : `대기 중 · ${provider}`;
  micRunning = state.mic_running;
  document.querySelector("#micBtn").textContent = micRunning ? "마이크 중지" : "마이크";
  if (state.ai_provider && document.activeElement !== analysisProvider) {
    analysisProvider.value = state.ai_provider;
  }
  renderLayout(state.layout || {});

  Object.values(state.panels).forEach(updatePanel);
  renderFeed(state.feed || []);

  const enabled = new Set(state.enabled_panels);
  document.querySelectorAll("[data-panel-toggle]").forEach((toggle) => {
    toggle.checked = enabled.has(toggle.dataset.panelToggle);
  });

  renderContext(state.context || {});

  transcript.innerHTML = "";
  state.transcript.forEach(addTranscript);
}

function renderLayout(layout) {
  const workspace = document.querySelector(".workspace");
  workspace.dataset.layoutMode = layout.mode || "normal";
  workspace.dataset.layoutSource = layout.source || "local";
  workspace.dataset.arbiter = layout.arbiter_enabled === false ? "off" : "on";
  layoutArbiterToggle.checked = layout.arbiter_enabled !== false;
  const spec = layout.ggui_spec || {};
  applyLayoutColumns(workspace, spec.columns);
  applyLayoutPanelVisuals(spec.panel_visuals || {});
  const source = layout.source === "exaone" ? "EXAONE UI Director" : "local";
  layoutArbiterToggle.parentElement.title = layout.reason
    ? `${source}: ${layout.reason}`
    : "위험 신호가 크면 C+ 영역을 자동 확장";
}

function applyLayoutColumns(workspace, columns) {
  if (!Array.isArray(columns) || columns.length !== 3) {
    workspace.style.removeProperty("--col-a");
    workspace.style.removeProperty("--col-b");
    workspace.style.removeProperty("--col-c");
    return;
  }
  const keys = ["--col-a", "--col-b", "--col-c"];
  columns.forEach((value, index) => {
    const number = Number(value);
    if (Number.isFinite(number)) {
      workspace.style.setProperty(keys[index], `${Math.max(0.4, Math.min(1.8, number))}fr`);
    }
  });
}

function applyLayoutPanelVisuals(panelVisuals) {
  const targets = {
    summarizer: document.querySelector("#panel-summarizer"),
    fact_checker: document.querySelector("#panel-fact_checker"),
    timeline: document.querySelector(".timeline-panel"),
  };
  Object.entries(targets).forEach(([key, node]) => {
    if (!node) return;
    const visual = panelVisuals[key] || {};
    applyVisualDataset(node, visual);
  });
}

function renderContext(context) {
  const terms = context.terms || [];
  const bits = [];
  if (context.topic) bits.push(context.topic);
  if (terms.length) bits.push(terms.slice(0, 4).join(", "));
  contextPill.textContent = bits.length ? `준비됨: ${bits.join(" · ")}` : "준비 문맥 없음";
  if (document.activeElement !== prepTopic) prepTopic.value = context.topic || "";
  if (document.activeElement !== prepGoal) prepGoal.value = context.goal || "";
  if (document.activeElement !== prepTerms) prepTerms.value = terms.join(", ");
}

function updatePanel(panel) {
  const node = document.querySelector(`#panel-${panel.panel_name}`);
  if (!node) return;

  const visual = panel.visual_spec || {};
  node.dataset.importance = String(panel.importance || 1);
  node.dataset.status = panel.status || "idle";
  applyVisualDataset(node, visual, panel);
  node.querySelector(".panel-text").textContent = panel.text || "";
  node.querySelector('[data-role="reason"]').textContent = panel.reason || "";
  renderPanelSources(node.querySelector('[data-role="sources"]'), panel.sources || []);

  const provider = node.querySelector('[data-role="provider"]');
  const elapsed = panel.elapsed_s ? `${Number(panel.elapsed_s).toFixed(1)}s` : "";
  provider.textContent = [panel.provider, elapsed].filter(Boolean).join(" · ");
}

function applyVisualDataset(node, visual = {}, fallback = {}) {
  node.dataset.tone = visual.tone || fallback.tone || "neutral";
  node.dataset.emphasis = visual.emphasis || fallback.emphasis || "none";
  node.dataset.density = visual.density || fallback.density || "normal";
  node.dataset.urgency = String(visual.urgency || fallback.urgency || 1);
  if (visual.component) node.dataset.component = visual.component;
}

function renderPanelSources(container, sources) {
  if (!container) return;
  container.innerHTML = "";
  container.dataset.visible = sources.length ? "true" : "false";
  sources.slice(0, 3).forEach((source, index) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = `${index + 1}. ${source.title || source.url}`;
    item.appendChild(link);
    container.appendChild(item);
  });
}

function renderFeed(items) {
  feedItemsById = new Map(items.map((item) => [String(item.id), item]));
  if (!items.length) {
    closeFeedDetail();
    insightFeed.innerHTML = "";
    const empty = document.createElement("li");
    empty.className = "feed-empty";
    empty.textContent = "인사이트 대기";
    insightFeed.appendChild(empty);
    return;
  }

  const empty = insightFeed.querySelector(".feed-empty");
  if (empty) empty.remove();

  const incomingIds = new Set(items.map((item) => String(item.id)));
  insightFeed.querySelectorAll(".feed-item").forEach((node) => {
    if (!incomingIds.has(node.dataset.feedId)) {
      node.remove();
    }
  });

  items.forEach((item) => {
    let node = insightFeed.querySelector(`[data-feed-id="${cssEscape(item.id)}"]`);
    if (node) {
      updateFeedNode(node, item);
    } else {
      node = createFeedNode(item);
    }
    insightFeed.appendChild(node);
  });
  syncFeedDetail();
  stickFeedToBottom();
}

function upsertFeedItem(item) {
  feedItemsById.set(String(item.id), item);
  const empty = insightFeed.querySelector(".feed-empty");
  if (empty) empty.remove();

  const existing = insightFeed.querySelector(`[data-feed-id="${cssEscape(item.id)}"]`);
  if (existing) {
    updateFeedNode(existing, item);
  } else {
    insightFeed.appendChild(createFeedNode(item));
  }
  syncFeedDetail();
  stickFeedToBottom();
}

function stickFeedToBottom() {
  requestAnimationFrame(() => {
    const last = insightFeed.lastElementChild;
    if (last) {
      last.scrollIntoView({ block: "end", inline: "nearest" });
    }
    insightFeedViewport.scrollTop = insightFeedViewport.scrollHeight;
  });
}

function createFeedNode(item) {
  const li = document.createElement("li");
  li.className = "feed-item";
  li.dataset.feedId = item.id;
  li.setAttribute("role", "button");
  li.tabIndex = 0;
  li.addEventListener("click", () => openFeedDetailById(li.dataset.feedId));
  li.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openFeedDetailById(li.dataset.feedId);
  });

  const top = document.createElement("div");
  top.className = "feed-top";
  const label = document.createElement("span");
  label.className = "feed-label";
  const time = document.createElement("span");
  time.className = "feed-time";
  top.append(label, time);

  const text = document.createElement("p");
  text.className = "feed-text";

  const meta = document.createElement("div");
  meta.className = "feed-meta";
  const reason = document.createElement("span");
  reason.dataset.role = "reason";
  const provider = document.createElement("span");
  provider.dataset.role = "provider";
  meta.append(reason, provider);

  const badges = document.createElement("div");
  badges.className = "feed-badges";

  li.append(top, text, badges, meta);
  updateFeedNode(li, item);
  return li;
}

function updateFeedNode(node, item) {
  const visual = item.visual_spec || {};
  node.dataset.feedId = item.id;
  node.dataset.panel = item.panel_name;
  node.dataset.importance = String(visual.importance || item.importance || 1);
  node.dataset.urgency = String(visual.urgency || item.urgency || 1);
  node.dataset.variant = visual.variant || item.card_variant || "note";
  node.dataset.tone = visual.tone || item.tone || "neutral";
  node.dataset.status = item.status || "idle";
  const isThinking = item.status === "thinking";
  node.dataset.clickable = isThinking ? "false" : "true";
  node.dataset.selected = activeFeedDetailId === String(item.id) ? "true" : "false";
  node.tabIndex = isThinking ? -1 : 0;
  node.setAttribute("aria-disabled", isThinking ? "true" : "false");
  node.setAttribute("aria-label", `${item.label || item.panel_name} ${isThinking ? "분석 중" : "상세 보기"}`);
  node.querySelector(".feed-label").textContent = item.label || item.panel_name;
  node.querySelector(".feed-time").textContent = formatTime(item.updated_at);
  node.querySelector(".feed-text").textContent = item.text || "";
  node.querySelector('[data-role="reason"]').textContent = item.reason || "";
  renderBadges(node.querySelector(".feed-badges"), visual.badges || item.badges || []);

  const elapsed = item.elapsed_s ? `${Number(item.elapsed_s).toFixed(1)}s` : "";
  node.querySelector('[data-role="provider"]').textContent = [item.provider, elapsed]
    .filter(Boolean)
    .join(" · ");
}

function renderBadges(container, badges) {
  container.innerHTML = "";
  badges.slice(0, 4).forEach((badge) => {
    const chip = document.createElement("span");
    chip.textContent = badge;
    container.appendChild(chip);
  });
}

function openFeedDetailById(feedId) {
  const item = feedItemsById.get(String(feedId));
  if (!item || item.status === "thinking") return;
  renderFeedDetail(item);
}

function renderFeedDetail(item) {
  if (!feedDetail) return;
  activeFeedDetailId = String(item.id);
  const visual = item.visual_spec || {};
  const points = Array.isArray(item.detail_points)
    ? item.detail_points.filter(Boolean)
    : [];
  const displayPoints = points.length ? points : [item.reason].filter(Boolean);
  const body = item.detail_body || item.text || "";
  const action = item.detail_action || "";

  feedDetail.dataset.active = "true";
  feedDetail.setAttribute("aria-hidden", "false");
  feedDetail.dataset.panel = item.panel_name || "";
  feedDetail.dataset.tone = visual.tone || item.tone || "neutral";
  feedDetailKicker.textContent = [item.label || item.panel_name, formatTime(item.updated_at)]
    .filter(Boolean)
    .join(" · ");
  feedDetailTitle.textContent = item.detail_title || item.label || "상세";
  feedDetailBody.textContent = body;
  feedDetailBody.hidden = !body;
  feedDetailPoints.innerHTML = "";
  displayPoints.slice(0, 5).forEach((point) => {
    const li = document.createElement("li");
    li.textContent = point;
    feedDetailPoints.appendChild(li);
  });
  feedDetailPoints.hidden = !displayPoints.length;
  feedDetailAction.textContent = action;
  feedDetailAction.hidden = !action;
  setActiveFeedCard(activeFeedDetailId);
}

function closeFeedDetail() {
  if (!feedDetail) return;
  activeFeedDetailId = null;
  feedDetail.dataset.active = "false";
  feedDetail.setAttribute("aria-hidden", "true");
  setActiveFeedCard(null);
}

function syncFeedDetail() {
  if (!activeFeedDetailId) return;
  const item = feedItemsById.get(activeFeedDetailId);
  if (!item || item.status === "thinking") {
    closeFeedDetail();
    return;
  }
  renderFeedDetail(item);
}

function setActiveFeedCard(feedId) {
  insightFeed.querySelectorAll(".feed-item").forEach((node) => {
    node.dataset.selected = feedId && node.dataset.feedId === feedId ? "true" : "false";
  });
}

function addTranscript(item) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = `${item.index} · ${item.timestamp.slice(11, 19)} · ${item.speaker}`;
  const text = document.createElement("span");
  text.textContent = item.text;
  li.append(time, text);
  transcript.appendChild(li);
  transcript.scrollTop = transcript.scrollHeight;
}

function typeConversation(item) {
  clearInterval(typingTimer);
  const text = item.text || "";
  const typingMs = Number(item.typing_ms || 32);
  let cursor = 0;

  conversationPopup.dataset.active = "true";
  conversationPopup.dataset.complete = "false";
  conversationText.textContent = "";

  typingTimer = setInterval(() => {
    cursor += 1;
    conversationText.textContent = text.slice(0, cursor);
    if (cursor >= text.length) {
      clearInterval(typingTimer);
      conversationPopup.dataset.complete = "true";
    }
  }, typingMs);
}

function finishConversation(stopped) {
  clearInterval(typingTimer);
  conversationPopup.dataset.complete = "true";
  if (stopped) {
    conversationPopup.dataset.active = "false";
    conversationText.textContent = "";
    return;
  }
  setTimeout(() => {
    conversationPopup.dataset.active = "false";
  }, 1800);
}

function formatTime(value) {
  if (!value) return "";
  const match = String(value).match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : "";
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/"/g, '\\"');
}

document.querySelector("#startBtn").addEventListener("click", () => send({ type: "start" }));
document.querySelector("#stopBtn").addEventListener("click", () => send({ type: "stop" }));
document.querySelector("#sampleBtn").addEventListener("click", () => send({ type: "play_sample" }));
document.querySelector("#resetBtn").addEventListener("click", () => send({ type: "reset" }));
document.querySelector("#demoBtn").addEventListener("click", () => {
  send({ type: "play_demo_script", mode: demoMode.value });
});
analysisProvider.addEventListener("change", () => {
  send({ type: "set_provider", provider: analysisProvider.value });
});
document.querySelector("#pauseDemoBtn").addEventListener("click", () => send({ type: "pause_demo_script" }));
document.querySelector("#resumeDemoBtn").addEventListener("click", () => send({ type: "resume_demo_script" }));
document.querySelector("#stopDemoBtn").addEventListener("click", () => send({ type: "stop_demo_script" }));
layoutArbiterToggle.addEventListener("change", () => {
  send({ type: "toggle_layout_arbiter", enabled: layoutArbiterToggle.checked });
});
if (feedDetailClose) {
  feedDetailClose.addEventListener("click", closeFeedDetail);
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && feedDetail?.dataset.active === "true") {
    closeFeedDetail();
  }
});
document.querySelector("#micBtn").addEventListener("click", () => {
  send({ type: micRunning ? "stop_mic" : "start_mic" });
});

prepForm.addEventListener("submit", (event) => {
  event.preventDefault();
  send({
    type: "prepare",
    topic: prepTopic.value.trim(),
    goal: prepGoal.value.trim(),
    terms: prepTerms.value.trim(),
  });
});

document.querySelectorAll("[data-panel-toggle]").forEach((toggle) => {
  toggle.addEventListener("change", () => {
    send({
      type: "toggle_panel",
      panel: toggle.dataset.panelToggle,
      enabled: toggle.checked,
    });
  });
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const provider = analysisProvider.value;
  send({ type: "utterance", text, provider });
  input.value = "";
  input.focus();
});

connect();
