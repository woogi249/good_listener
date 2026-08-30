const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ACTIVE_MEETING_KEY = "good-listener.active-meeting";
const AUDIO_DB_NAME = "good-listener-audio-v1";
const AUDIO_STORE_NAME = "chunks";
const AUDIO_TIMESLICE_MS = 5_000;
const WS_RETRY_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
const API_REQUEST_TIMEOUT_MS = 15_000;
const AUDIO_UPLOAD_TIMEOUT_MS = 10_000;
const FINAL_AUDIO_FLUSH_TIMEOUT_MS = 15_000;

const dom = {
  body: document.body,
  setupView: $("#setupView"),
  meetingView: $("#meetingView"),
  finalizingView: $("#finalizingView"),
  minutesView: $("#minutesView"),
  setupForm: $("#setupForm"),
  setupStartBtn: $("#setupStartBtn"),
  startBtn: $("#startBtn"),
  pauseBtn: $("#pauseBtn"),
  stopBtn: $("#stopBtn"),
  transcriptBtn: $("#transcriptBtn"),
  meetingTopic: $("#meetingTopic"),
  meetingGoal: $("#meetingGoal"),
  meetingTerms: $("#meetingTerms"),
  consentCheck: $("#consentCheck"),
  audioDevice: $("#audioDevice"),
  micTestBtn: $("#micTestBtn"),
  micTestStatus: $("#micTestStatus"),
  micMeterBar: $("#micMeterBar"),
  meetingTitleHeader: $("#meetingTitleHeader"),
  meetingTimer: $("#meetingTimer"),
  appConnectionPill: $("#appConnectionPill"),
  appConnectionText: $("#appConnectionText"),
  micStatePill: $("#micStatePill"),
  micStateText: $("#micStateText"),
  analysisStatePill: $("#analysisStatePill"),
  analysisStateText: $("#analysisStateText"),
  systemBanner: $("#systemBanner"),
  systemBannerTitle: $("#systemBannerTitle"),
  systemBannerMessage: $("#systemBannerMessage"),
  systemBannerAction: $("#systemBannerAction"),
  captionState: $("#captionState"),
  captionFinals: $("#captionFinals"),
  captionPartial: $("#captionPartial"),
  captionAnnouncement: $("#captionAnnouncement"),
  progressUpdating: $("#progressUpdating"),
  currentTopic: $("#currentTopic"),
  currentSummary: $("#currentSummary"),
  agendaList: $("#agendaList"),
  progressUpdatedAt: $("#progressUpdatedAt"),
  progressEvidenceBtn: $("#progressEvidenceBtn"),
  factViewport: $("#factViewport"),
  factList: $("#factList"),
  factPendingCount: $("#factPendingCount"),
  factVerifiedCount: $("#factVerifiedCount"),
  factContradictedCount: $("#factContradictedCount"),
  factInconclusiveCount: $("#factInconclusiveCount"),
  newFactsBtn: $("#newFactsBtn"),
  decisionCount: $("#decisionCount"),
  actionCount: $("#actionCount"),
  openQuestionCount: $("#openQuestionCount"),
  suggestionCount: $("#suggestionCount"),
  insightDrawer: $("#insightDrawer"),
  drawerTitle: $("#drawerTitle"),
  drawerList: $("#drawerList"),
  transcriptDrawer: $("#transcriptDrawer"),
  transcriptList: $("#transcriptList"),
  newTranscriptBtn: $("#newTranscriptBtn"),
  stopDialog: $("#stopDialog"),
  confirmStopBtn: $("#confirmStopBtn"),
  deleteDialog: $("#deleteDialog"),
  deleteConfirmInput: $("#deleteConfirmInput"),
  confirmDeleteBtn: $("#confirmDeleteBtn"),
  finalizingMessage: $("#finalizingMessage"),
  finalizationSteps: $("#finalizationSteps"),
  minutesTitle: $("#minutesTitle"),
  minutesPhaseLabel: $("#minutesPhaseLabel"),
  minutesMeta: $("#minutesMeta"),
  minutesMarkdown: $("#minutesMarkdown"),
  minutesPrintContent: $("#minutesPrintContent"),
  saveMinutesBtn: $("#saveMinutesBtn"),
  approveMinutesBtn: $("#approveMinutesBtn"),
  downloadMinutesBtn: $("#downloadMinutesBtn"),
  printMinutesBtn: $("#printMinutesBtn"),
  deleteMeetingBtn: $("#deleteMeetingBtn"),
  speakerList: $("#speakerList"),
  speakerCount: $("#speakerCount"),
  reviewIssueList: $("#reviewIssueList"),
  reviewIssueCount: $("#reviewIssueCount"),
  developerTools: $("#developerTools"),
  toastRegion: $("#toastRegion"),
};

const state = {
  phase: "setup",
  phaseBeforeReconnect: "live",
  meetingId: null,
  meeting: null,
  revision: 0,
  lastSeq: 0,
  seenEventIds: new Set(),
  socket: null,
  socketIntentionalClose: false,
  socketRetryAttempt: 0,
  socketRetryTimer: null,
  socketStatus: "pending",
  socketAuthRefreshAttempted: false,
  controlRefreshPromise: null,
  pendingSocketMessages: new Map(),
  snapshotRefreshPromise: null,
  mediaStream: null,
  mediaRecorder: null,
  audioSequence: 0,
  audioChunkStartedAt: null,
  audioPersistChain: Promise.resolve(),
  audioFlushPromise: null,
  audioUploadController: null,
  audioSyncPending: false,
  memoryAudioQueue: new Map(),
  peer: null,
  realtimeChannel: null,
  realtimeIntentionalClose: false,
  realtimeRetryAttempt: 0,
  realtimeRetryTimer: null,
  realtimeStatus: "idle",
  finalizedIds: new Set(),
  transcriptArrivalSeq: 0,
  realtimeItemPrevious: new Map(),
  partials: new Map(),
  transcript: [],
  progress: {},
  facts: [],
  decisions: [],
  actions: [],
  openQuestions: [],
  suggestions: [],
  pendingJobs: {},
  minutes: null,
  minutesStale: false,
  speakerNames: {},
  activeDrawer: null,
  drawerOpener: null,
  transcriptOpener: null,
  unreadFacts: 0,
  unreadTranscript: 0,
  lastFactIds: new Set(),
  lastTranscriptIds: new Set(),
  minutesPollTimer: null,
  timerHandle: null,
  startedAt: null,
  endedAt: null,
  bannerAction: null,
  bannerContext: "",
  busy: false,
  deletionInProgress: false,
};

function makeId(prefix = "evt") {
  if (crypto.randomUUID) return `${prefix}-${crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function parseTerms(value) {
  return String(value || "")
    .split(/[\n,]/)
    .map((term) => term.trim())
    .filter(Boolean)
    .slice(0, 80);
}

function getText(value, fallback = "") {
  if (typeof value === "string") return value.trim();
  if (!value || typeof value !== "object") return fallback;
  return String(
    value.text ?? value.title ?? value.summary ?? value.description ?? value.content ?? fallback,
  ).trim();
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function formatClock(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return [hours, minutes, rest].map((part) => String(part).padStart(2, "0")).join(":");
}

function formatLocalTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatRelativeTime(value) {
  if (!value) return "업데이트 대기";
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "업데이트 대기";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 10) return "방금 업데이트";
  if (seconds < 60) return `${seconds}초 전 업데이트`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 전 업데이트`;
  return `${Math.floor(minutes / 60)}시간 전 업데이트`;
}

function sanitizeFilename(value) {
  return String(value || "meeting")
    .replace(/[\\/:*?"<>|]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "meeting";
}

function isSafeUrl(value) {
  try {
    const url = new URL(value, location.origin);
    return ["http:", "https:"].includes(url.protocol);
  } catch {
    return false;
  }
}

function isNearBottom(element, threshold = 64) {
  if (!element) return true;
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function updateTimer() {
  if (!state.startedAt) {
    dom.meetingTimer.textContent = "00:00:00";
    dom.meetingTimer.dateTime = "PT0S";
    return;
  }
  const end = state.endedAt ? new Date(state.endedAt).getTime() : Date.now();
  const start = new Date(state.startedAt).getTime();
  const seconds = Number.isFinite(start) ? Math.max(0, Math.floor((end - start) / 1000)) : 0;
  dom.meetingTimer.textContent = formatClock(seconds);
  dom.meetingTimer.dateTime = `PT${seconds}S`;
}

function setPill(node, textNode, status, text) {
  node.dataset.status = status;
  textNode.textContent = text;
}

function showBanner(title, message, options = {}) {
  dom.systemBanner.hidden = false;
  dom.systemBanner.dataset.tone = options.tone || "warning";
  dom.systemBanner.setAttribute("aria-live", options.assertive ? "assertive" : "polite");
  dom.systemBannerTitle.textContent = title;
  dom.systemBannerMessage.textContent = message || "";
  state.bannerAction = typeof options.action === "function" ? options.action : null;
  state.bannerContext = options.context || "";
  dom.systemBannerAction.textContent = options.actionLabel || "다시 시도";
  dom.systemBannerAction.hidden = !state.bannerAction;
}

function hideBanner() {
  dom.systemBanner.hidden = true;
  state.bannerAction = null;
  state.bannerContext = "";
  dom.systemBannerAction.hidden = true;
}

function showToast(message, tone = "neutral") {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.tone = tone;
  toast.textContent = message;
  dom.toastRegion.replaceChildren(toast);
  window.setTimeout(() => {
    if (toast.isConnected) toast.remove();
  }, 3_500);
}

function normalizePhase(value) {
  const phase = String(value || "").toLowerCase();
  if (["created", "ready", "setup", "idle"].includes(phase)) return "connecting";
  if (["starting", "connecting"].includes(phase)) return "connecting";
  if (["running", "live", "active", "listening"].includes(phase)) return "live";
  if (["paused", "pause"].includes(phase)) return "paused";
  if (["reconnecting", "recovering"].includes(phase)) return "reconnecting";
  if (["stopping", "finalizing", "processing"].includes(phase)) return "finalizing";
  if (["review", "draft", "minutes_ready"].includes(phase)) return "review";
  if (["completed", "approved", "done"].includes(phase)) return "completed";
  if (["failed", "error"].includes(phase)) return "error";
  return state.phase;
}

function setPhase(nextPhase, options = {}) {
  const phase = [
    "setup",
    "connecting",
    "live",
    "paused",
    "reconnecting",
    "finalizing",
    "review",
    "completed",
    "error",
  ].includes(nextPhase)
    ? nextPhase
    : "error";

  state.phase = phase;
  dom.body.dataset.phase = phase;

  const hasMeeting = Boolean(state.meetingId);
  const setupVisible = phase === "setup" || phase === "connecting" || (phase === "error" && !hasMeeting);
  const meetingVisible = ["live", "paused", "reconnecting"].includes(phase) || (phase === "error" && hasMeeting && !state.minutes);
  const finalizingVisible = phase === "finalizing";
  const minutesVisible = ["review", "completed"].includes(phase);

  dom.setupView.hidden = !setupVisible;
  dom.setupForm.setAttribute("aria-busy", String(phase === "connecting"));
  dom.meetingView.hidden = !meetingVisible;
  dom.finalizingView.hidden = !finalizingVisible;
  dom.minutesView.hidden = !minutesVisible;

  dom.startBtn.hidden = !["setup", "connecting", "review", "completed", "error"].includes(phase);
  const meetingChangeLocked = ["review", "completed"].includes(phase) && state.audioSyncPending;
  dom.startBtn.disabled = phase === "connecting" || state.busy || meetingChangeLocked;
  dom.startBtn.textContent = ["review", "completed"].includes(phase)
    ? "새 회의"
    : phase === "connecting"
      ? "연결 중"
      : phase === "error" && hasMeeting
        ? "다시 연결"
        : "회의 시작";
  dom.startBtn.title = meetingChangeLocked ? "남은 음성 업로드가 끝나면 새 회의를 시작할 수 있습니다." : "";

  dom.pauseBtn.hidden = !["live", "paused", "reconnecting"].includes(phase);
  dom.pauseBtn.disabled = phase === "reconnecting" || state.busy;
  dom.pauseBtn.textContent = phase === "paused" ? "재개" : "일시정지";
  dom.stopBtn.hidden = !["live", "paused", "reconnecting"].includes(phase);
  dom.stopBtn.disabled = state.busy;
  dom.transcriptBtn.hidden = !hasMeeting || phase === "setup" || phase === "connecting";

  const micStatus = {
    setup: ["idle", "마이크 대기"],
    connecting: ["pending", "마이크 연결 중"],
    live: ["live", "듣는 중"],
    paused: ["warning", "마이크 일시정지"],
    reconnecting: ["warning", "전사 재연결 중"],
    finalizing: ["idle", "음성 수집 종료"],
    review: ["idle", "음성 수집 종료"],
    completed: ["idle", "음성 수집 종료"],
    error: ["error", hasMeeting ? "마이크 확인 필요" : "마이크 대기"],
  }[phase];
  setPill(dom.micStatePill, dom.micStateText, micStatus[0], micStatus[1]);

  dom.captionState.textContent = phase === "paused" ? "일시정지" : phase === "reconnecting" ? "재연결 중" : "듣는 중";
  dom.captionState.dataset.status = phase;

  if (!options.preserveFocus) {
    const target = setupVisible ? $("#setupTitle") : finalizingVisible ? $("#finalizingTitle") : minutesVisible ? dom.minutesTitle : null;
    if (target && options.focus) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
  }
  renderHealth();
}

function renderHealth() {
  const socketMap = {
    pending: ["pending", "서버 확인 중"],
    connecting: ["pending", "서버 연결 중"],
    open: ["ok", "앱 연결됨"],
    reconnecting: ["reconnecting", "앱 재연결 중"],
    offline: ["error", "네트워크 끊김"],
    error: ["error", "서버 연결 오류"],
  };
  const socket = socketMap[state.socketStatus] || socketMap.pending;
  setPill(dom.appConnectionPill, dom.appConnectionText, socket[0], socket[1]);

  const pending = pendingJobCount(state.pendingJobs);
  const realtimeWorking = ["connecting", "reconnecting"].includes(state.realtimeStatus);
  if (state.phase === "finalizing") {
    setPill(dom.analysisStatePill, dom.analysisStateText, "warning", "회의록 작성 중");
  } else if (realtimeWorking) {
    setPill(dom.analysisStatePill, dom.analysisStateText, "warning", "전사 연결 중");
  } else if (pending > 0) {
    setPill(dom.analysisStatePill, dom.analysisStateText, "warning", `분석 ${pending}건 처리 중`);
  } else if (["live", "paused", "review", "completed"].includes(state.phase)) {
    setPill(dom.analysisStatePill, dom.analysisStateText, "ok", "분석 정상");
  } else {
    setPill(dom.analysisStatePill, dom.analysisStateText, "idle", "분석 대기");
  }
}

function pendingJobCount(jobs) {
  if (Array.isArray(jobs)) return jobs.filter((job) => !["complete", "completed", "failed"].includes(String(job.status))).length;
  if (!jobs || typeof jobs !== "object") return Number(jobs) || 0;
  return Object.values(jobs).reduce((total, value) => {
    if (typeof value === "number") return total + Math.max(0, value);
    if (typeof value === "boolean") return total + (value ? 1 : 0);
    if (value && typeof value === "object") {
      const status = String(value.status || "");
      const count = Number(value.count ?? value.pending ?? 0);
      return total + (count || (!["complete", "completed", "failed"].includes(status) ? 1 : 0));
    }
    return total;
  }, 0);
}

async function api(path, options = {}) {
  const {
    timeoutMs = API_REQUEST_TIMEOUT_MS,
    signal: callerSignal,
    retryControlSession = true,
    ...fetchOptions
  } = options;
  const headers = new Headers(fetchOptions.headers || {});
  const isForm = fetchOptions.body instanceof FormData;
  if (fetchOptions.body && !isForm && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, Math.max(1, Number(timeoutMs) || API_REQUEST_TIMEOUT_MS));

  try {
    const response = await fetch(path, {
      ...fetchOptions,
      headers,
      signal: controller.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
    const requestUrl = new URL(path, location.origin);
    if (
      response.status === 403
      && retryControlSession
      && requestUrl.origin === location.origin
      && requestUrl.pathname !== "/api/bootstrap"
    ) {
      await refreshControlSession();
      return api(path, { ...options, retryControlSession: false });
    }
    if (!response.ok) {
      let detail = "";
      try {
        const body = await response.json();
        detail = body.detail || body.message || body.error || "";
      } catch {
        detail = await response.text().catch(() => "");
      }
      const error = new Error(detail || `요청 실패 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : response.text();
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(timedOut ? "서버 응답 시간이 초과되었습니다." : "요청이 취소되었습니다.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

function refreshControlSession() {
  if (state.controlRefreshPromise) return state.controlRefreshPromise;
  state.controlRefreshPromise = api("/api/bootstrap", {
    retryControlSession: false,
  }).finally(() => {
    state.controlRefreshPromise = null;
  });
  return state.controlRefreshPromise;
}

function meetingPath(suffix = "") {
  if (!state.meetingId) throw new Error("활성 회의가 없습니다.");
  return `/api/meetings/${encodeURIComponent(state.meetingId)}${suffix}`;
}

function rememberMeeting(id) {
  state.meetingId = id || null;
  if (id) localStorage.setItem(ACTIVE_MEETING_KEY, id);
  else localStorage.removeItem(ACTIVE_MEETING_KEY);
}

function audioSequenceKey(meetingId) {
  return `good-listener.audio-sequence.${meetingId}`;
}

function applySnapshot(raw, options = {}) {
  if (!raw || typeof raw !== "object") return;
  const snapshot = raw.snapshot || raw.state || raw;
  const meeting = snapshot.meeting || raw.meeting || state.meeting || {};
  const id = meeting.id || snapshot.meeting_id || raw.meeting_id || state.meetingId;
  if (id) rememberMeeting(String(id));

  state.meeting = { ...(state.meeting || {}), ...meeting };
  state.revision = Math.max(state.revision, Number(snapshot.revision ?? meeting.revision ?? 0));
  state.startedAt = meeting.started_at || snapshot.started_at || state.startedAt;
  state.endedAt = meeting.ended_at || snapshot.ended_at || state.endedAt;
  state.pendingJobs = snapshot.pending_jobs || meeting.pending_jobs || state.pendingJobs || {};

  const topic = snapshot.current_topic ?? snapshot.topic ?? meeting.topic;
  if (topic !== undefined) state.currentTopic = topic;
  if (snapshot.progress !== undefined) {
    state.progress = Array.isArray(snapshot.progress)
      ? {
          ...(Array.isArray(state.progress) ? {} : state.progress),
          agenda_items: snapshot.progress,
          current_topic: snapshot.current_topic || topic || "",
          evidence_utterance_ids: snapshot.current_topic_evidence_ids || [],
          updated_at: snapshot.updated_at || meeting.updated_at || "",
          updating: false,
        }
      : snapshot.progress || {};
  }
  if (snapshot.facts !== undefined) state.facts = safeArray(snapshot.facts);
  if (snapshot.decisions !== undefined) state.decisions = safeArray(snapshot.decisions);
  if (snapshot.action_items !== undefined || snapshot.actions !== undefined) {
    state.actions = safeArray(snapshot.action_items ?? snapshot.actions);
  }
  if (snapshot.open_questions !== undefined) state.openQuestions = safeArray(snapshot.open_questions);
  if (snapshot.suggestions !== undefined) state.suggestions = safeArray(snapshot.suggestions);

  if (snapshot.transcript !== undefined) replaceTranscript(safeArray(snapshot.transcript));
  if (snapshot.minutes !== undefined) {
    state.minutes = snapshot.minutes;
    state.minutesStale = String(snapshot.minutes?.status || "").toLowerCase() === "stale";
  }

  const lifecycle = snapshot.lifecycle || meeting.lifecycle || state.phase;
  let nextPhase = normalizePhase(lifecycle);
  if (nextPhase === "finalizing" && state.minutes) state.minutesStale = true;
  const minuteStatus = String(state.minutes?.status || "").toLowerCase();
  const lifecycleOwnsView = ["connecting", "live", "paused", "reconnecting", "finalizing", "error"].includes(nextPhase);
  if (!lifecycleOwnsView && state.minutes && ["approved", "completed", "final"].includes(minuteStatus)) nextPhase = "completed";
  else if (!lifecycleOwnsView && state.minutes && !["pending", "generating", "stale"].includes(minuteStatus)) nextPhase = "review";
  if (options.keepConnecting) nextPhase = "connecting";

  renderMeetingData();
  if (state.minutes) renderMinutes();
  if (nextPhase !== "connecting" || !options.keepConnecting) setPhase(nextPhase);
  if (nextPhase === "finalizing") startMinutesPolling();
  if (["review", "completed"].includes(nextPhase) && !state.minutesStale && state.bannerContext === "minutes-stale") hideBanner();
  if (nextPhase === "error" && String(lifecycle).toLowerCase() === "failed") {
    showBanner("회의록 생성 중 문제가 발생했습니다", meeting.finalization_error || "저장된 전사와 음성으로 다시 처리할 수 있습니다.", {
      tone: "danger",
      assertive: true,
      actionLabel: "회의록 다시 만들기",
      action: retryFinalization,
    });
  }
}

function applyLegacyState(legacy) {
  if (!legacy || typeof legacy !== "object") return;
  const context = legacy.context || {};
  state.meeting = { ...(state.meeting || {}), topic: context.topic || state.meeting?.topic || "회의" };
  state.currentTopic = context.topic || state.currentTopic;
  const summary = legacy.panels?.summarizer;
  if (summary && summary.status !== "thinking") {
    state.progress = {
      ...state.progress,
      summary: summary.text,
      updated_at: summary.updated_at,
      updating: false,
    };
  } else if (summary?.status === "thinking") {
    state.progress = { ...state.progress, updating: true };
  }
  const fact = legacy.panels?.fact_checker;
  if (fact?.text && !fact.text.includes("대기")) {
    state.facts = [{
      id: "legacy-fact",
      claim: fact.reason || "최근 주장",
      verdict: fact.text,
      status: fact.status === "thinking" ? "searching" : fact.text.startsWith("틀림") ? "contradicted" : fact.text.startsWith("맞음") ? "verified" : "inconclusive",
      sources: fact.sources || [],
      updated_at: fact.updated_at,
    }];
  }
  replaceTranscript(safeArray(legacy.transcript));
  state.suggestions = safeArray(legacy.feed);
  setPhase(legacy.running ? "live" : "setup");
  renderMeetingData();
}

async function refreshSnapshot() {
  if (!state.meetingId) return null;
  if (state.snapshotRefreshPromise) return state.snapshotRefreshPromise;
  state.snapshotRefreshPromise = api(meetingPath())
    .then((snapshot) => {
      applySnapshot(snapshot);
      return snapshot;
    })
    .finally(() => {
      state.snapshotRefreshPromise = null;
    });
  return state.snapshotRefreshPromise;
}

function socketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const path = `/ws/meetings/${encodeURIComponent(state.meetingId)}`;
  const url = new URL(`${protocol}//${location.host}${path}`);
  url.searchParams.set("after_revision", String(state.revision || 0));
  return url.toString();
}

function connectMeetingSocket() {
  if (!state.meetingId) return Promise.reject(new Error("회의 ID가 없습니다."));
  clearTimeout(state.socketRetryTimer);
  if (state.socket?.readyState === WebSocket.OPEN) return Promise.resolve();
  if (state.socket?.readyState === WebSocket.CONNECTING) {
    const existing = state.socket;
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error("회의 서버 연결 시간이 초과되었습니다.")), 10_000);
      existing.addEventListener("open", () => {
        clearTimeout(timeout);
        resolve();
      }, { once: true });
      existing.addEventListener("close", () => {
        clearTimeout(timeout);
        reject(new Error("회의 서버 연결이 완료되기 전에 닫혔습니다."));
      }, { once: true });
    });
  }
  state.socketIntentionalClose = false;
  state.socketStatus = "connecting";
  renderHealth();

  return new Promise((resolve, reject) => {
    const socket = new WebSocket(socketUrl());
    state.socket = socket;
    let settled = false;

    socket.addEventListener("open", async () => {
      state.socketStatus = "open";
      state.socketRetryAttempt = 0;
      state.socketAuthRefreshAttempted = false;
      renderHealth();
      flushSocketMessages();
      flushAudioQueue();
      try {
        await refreshSnapshot();
      } catch {
        // The durable WebSocket stream can still restore state.
      }
      if (state.phase === "reconnecting" && state.realtimeStatus === "open") {
        setPhase(state.phaseBeforeReconnect === "paused" ? "paused" : "live");
      }
      if (state.bannerContext === "socket-recovery") hideBanner();
      settled = true;
      resolve();
    });

    socket.addEventListener("message", (event) => {
      try {
        handleSocketEvent(JSON.parse(event.data));
      } catch (error) {
        showToast(`서버 이벤트를 읽지 못했습니다: ${error.message}`, "danger");
      }
    });

    socket.addEventListener("error", () => {
      state.socketStatus = "error";
      renderHealth();
      if (!settled) {
        settled = true;
        reject(new Error("회의 서버에 연결하지 못했습니다."));
      }
    });

    socket.addEventListener("close", (event) => {
      if (state.socket === socket) state.socket = null;
      if (state.socketIntentionalClose) return;
      state.socketStatus = navigator.onLine ? "reconnecting" : "offline";
      renderHealth();
      if (event.code === 4403 && !state.socketAuthRefreshAttempted) {
        state.socketAuthRefreshAttempted = true;
        const recovered = refreshControlSession().then(() => connectMeetingSocket());
        if (!settled) {
          recovered.then(() => {
            settled = true;
            resolve();
          }).catch((error) => {
            state.socketAuthRefreshAttempted = false;
            settled = true;
            reject(error);
            scheduleSocketReconnect();
          });
        } else {
          recovered.catch(() => {
            state.socketAuthRefreshAttempted = false;
            scheduleSocketReconnect();
          });
        }
        return;
      }
      if (!settled) {
        settled = true;
        reject(new Error(`회의 서버 연결이 닫혔습니다 (${event.code || "unknown"}).`));
      }
      scheduleSocketReconnect();
    });
  });
}

function scheduleSocketReconnect() {
  if (!state.meetingId || state.socketIntentionalClose || state.socketRetryTimer) return;
  const active = ["live", "paused", "reconnecting", "finalizing", "review", "completed", "error"].includes(state.phase);
  if (!active) return;
  if (["live", "paused"].includes(state.phase)) {
    state.phaseBeforeReconnect = state.phase;
    setPhase("reconnecting");
  }
  if (!["minutes-stale", "microphone-restore"].includes(state.bannerContext)) {
    showBanner(
      navigator.onLine ? "앱 연결을 복구하고 있습니다" : "네트워크 연결이 끊겼습니다",
      "음성은 이 기기에 임시 보관하고, 연결되면 순서대로 업로드합니다.",
      { actionLabel: "지금 다시 연결", action: reconnectAll, context: "socket-recovery" },
    );
  }
  const index = Math.min(state.socketRetryAttempt, WS_RETRY_MS.length - 1);
  const jitter = Math.floor(Math.random() * 350);
  state.socketRetryTimer = window.setTimeout(() => {
    state.socketRetryTimer = null;
    state.socketRetryAttempt += 1;
    connectMeetingSocket().catch(scheduleSocketReconnect);
  }, WS_RETRY_MS[index] + jitter);
}

function closeMeetingSocket() {
  state.socketIntentionalClose = true;
  clearTimeout(state.socketRetryTimer);
  state.socketRetryTimer = null;
  if (state.socket) state.socket.close(1000, "client close");
  state.socket = null;
}

function socketMessageKey(type, payload) {
  const nested = payload.payload || {};
  return `${type}:${payload.item_id || payload.chunk_id || nested.item_id || nested.chunk_id || payload.client_event_id || makeId("msg")}`;
}

function sendMeetingEvent(type, payload = {}, options = {}) {
  const message = { type, ...payload };
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(message));
    return true;
  }
  if (options.queue !== false) {
    state.pendingSocketMessages.set(socketMessageKey(type, payload), message);
  }
  return false;
}

function flushSocketMessages() {
  if (state.socket?.readyState !== WebSocket.OPEN) return;
  for (const [key, message] of state.pendingSocketMessages) {
    state.socket.send(JSON.stringify(message));
    state.pendingSocketMessages.delete(key);
  }
}

function handleSocketEvent(event) {
  if (!event || typeof event !== "object") return;
  if (event.type === "state" && event.state) {
    applyLegacyState(event.state);
    return;
  }
  if (event.type === "transcript" && event.item) {
    handleFinalTranscript(event.item, false);
    return;
  }
  if (event.type === "panel_update" || event.type === "feed_update") {
    handleLegacyUpdate(event);
    return;
  }
  if (event.type === "status") {
    showToast(event.message || "상태가 변경되었습니다.");
    return;
  }

  const eventId = event.event_id;
  if (eventId && state.seenEventIds.has(eventId)) return;
  if (eventId) {
    state.seenEventIds.add(eventId);
    if (state.seenEventIds.size > 2_000) state.seenEventIds = new Set([...state.seenEventIds].slice(-1_000));
  }

  const seq = Number(event.seq || 0);
  const revision = Number(event.revision || 0);
  if (seq && state.lastSeq && seq > state.lastSeq + 1) refreshSnapshot().catch(() => {});
  state.lastSeq = Math.max(state.lastSeq, seq);
  state.revision = Math.max(state.revision, revision);

  const payload = event.payload || {};
  switch (event.type) {
    case "transcript.partial":
      handlePartialTranscript(payload, false);
      break;
    case "transcript.final":
    case "transcript.corrected":
      handleFinalTranscript(payload, false);
      break;
    case "progress.updated":
    case "meeting.progress.updated":
      state.progress = { ...state.progress, ...payload, updating: false };
      renderProgress();
      break;
    case "fact.queued":
    case "fact.created":
    case "fact.updated":
    case "meeting.fact.updated":
      upsertFact(payload);
      break;
    case "meeting.state_updated": {
      const analysisState = payload.data || payload;
      applySnapshot({
        meeting: state.meeting,
        lifecycle: state.phase,
        revision: event.revision,
        ...analysisState,
      });
      break;
    }
    case "meeting.lifecycle_changed":
      if (payload.to === "finalizing" && state.minutes) state.minutesStale = true;
      if (!(state.busy && state.phase === "connecting" && payload.to === "live")) {
        setPhase(normalizePhase(payload.to || payload.lifecycle));
      }
      if (payload.to === "finalizing") startMinutesPolling();
      if (payload.to === "failed") {
        showBanner("회의록 생성 중 문제가 발생했습니다", payload.error || "저장된 데이터로 다시 처리할 수 있습니다.", {
          tone: "danger",
          assertive: true,
          actionLabel: "회의록 다시 만들기",
          action: retryFinalization,
        });
      }
      break;
    case "realtime.status":
      state.realtimeStatus = payload.status === "connected" ? "open" : payload.status || state.realtimeStatus;
      renderHealth();
      break;
    case "finalization.updated":
      state.pendingJobs = payload.pending_jobs || payload.jobs || payload;
      renderFinalization();
      renderHealth();
      break;
    case "minutes.ready":
      state.minutes = payload.minutes || payload;
      state.minutesStale = false;
      renderMinutes();
      setPhase(String(state.minutes.status).toLowerCase() === "approved" ? "completed" : "review", { focus: true });
      stopMinutesPolling();
      if (state.bannerContext === "minutes-stale") hideBanner();
      break;
    case "minutes.approved":
      state.minutes = payload.minutes || payload;
      state.minutesStale = false;
      renderMinutes();
      setPhase("completed", { focus: true });
      break;
    case "minutes.stale":
      state.minutes = payload.minutes || { ...(state.minutes || {}), status: "stale" };
      state.minutesStale = true;
      renderMinutes();
      setPhase("finalizing", { focus: true });
      showBanner("새 음성을 회의록에 반영하고 있습니다", "기존 회의록은 잠시 잠겼습니다. 최신 음성과 팩트가 반영되면 다시 검토할 수 있습니다.", {
        context: "minutes-stale",
      });
      renderFinalization();
      startMinutesPolling();
      break;
    case "meeting.deleted":
      handleRemoteDeletion();
      break;
    case "error":
      showBanner("처리 중 문제가 발생했습니다", payload.message || payload.detail || "다시 시도해 주세요.", {
        tone: "danger",
        assertive: true,
        action: reconnectAll,
      });
      break;
    default:
      if (payload.snapshot || payload.meeting || payload.lifecycle || payload.transcript || payload.progress) {
        applySnapshot(payload);
      } else if (event.snapshot || event.meeting || event.lifecycle) {
        applySnapshot(event);
      }
      break;
  }
}

function handleLegacyUpdate(event) {
  if (event.type === "panel_update" && event.panel?.panel_name === "summarizer") {
    if (event.panel.status !== "thinking") {
      state.progress = {
        ...state.progress,
        summary: event.panel.text,
        updated_at: event.panel.updated_at,
        updating: false,
      };
    } else {
      state.progress = { ...state.progress, updating: true };
    }
    renderProgress();
  } else if (event.type === "panel_update" && event.panel?.panel_name === "fact_checker") {
    upsertFact({
      id: "legacy-fact",
      claim: event.panel.reason || "최근 주장",
      verdict: event.panel.text,
      status: event.panel.status === "thinking" ? "searching" : "inconclusive",
      sources: event.panel.sources,
      updated_at: event.panel.updated_at,
    });
  } else if (event.type === "feed_update" && event.item) {
    state.suggestions = [...state.suggestions.filter((item) => item.id !== event.item.id), event.item];
    renderLedger();
  }
}

async function requestMicrophone() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("이 브라우저는 마이크 입력을 지원하지 않습니다. HTTPS 또는 localhost에서 열어 주세요.");
  }
  if (state.mediaStream?.getAudioTracks().some((track) => track.readyState === "live")) return state.mediaStream;
  const deviceId = dom.audioDevice.value;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      deviceId: deviceId ? { exact: deviceId } : undefined,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
    video: false,
  });
  state.mediaStream = stream;
  for (const track of stream.getAudioTracks()) {
    track.addEventListener("ended", handleMicrophoneEnded, { once: true });
  }
  return stream;
}

function handleMicrophoneEnded() {
  if (!["live", "reconnecting"].includes(state.phase)) return;
  setPhase("error");
  showBanner("마이크 연결이 끊겼습니다", "입력 장치를 확인한 뒤 다시 연결해 주세요. 기존 회의 기록은 유지됩니다.", {
    tone: "danger",
    assertive: true,
    actionLabel: "마이크 다시 연결",
    action: reconnectAll,
  });
}

async function populateAudioDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const selected = dom.audioDevice.value;
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "audioinput");
  dom.audioDevice.replaceChildren();
  const base = document.createElement("option");
  base.value = "";
  base.textContent = "기본 마이크";
  dom.audioDevice.appendChild(base);
  devices.forEach((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = device.label || `마이크 ${index + 1}`;
    dom.audioDevice.appendChild(option);
  });
  if ([...dom.audioDevice.options].some((option) => option.value === selected)) dom.audioDevice.value = selected;
}

async function testMicrophone() {
  if (!dom.consentCheck.checked) {
    dom.consentCheck.focus();
    dom.consentCheck.reportValidity();
    return;
  }
  dom.micTestBtn.disabled = true;
  dom.micTestStatus.textContent = "마이크 권한을 확인하고 있습니다…";
  let stream;
  let context;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: dom.audioDevice.value ? { exact: dom.audioDevice.value } : undefined },
      video: false,
    });
    await populateAudioDevices();
    context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    const values = new Uint8Array(analyser.frequencyBinCount);
    const started = performance.now();
    let peak = 0;
    await new Promise((resolve) => {
      function sample(now) {
        analyser.getByteTimeDomainData(values);
        const rms = Math.sqrt(values.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / values.length);
        peak = Math.max(peak, rms);
        dom.micMeterBar.style.setProperty("--mic-level", `${Math.min(100, rms * 320)}%`);
        if (now - started < 2_500) requestAnimationFrame(sample);
        else resolve();
      }
      requestAnimationFrame(sample);
    });
    dom.micTestStatus.textContent = peak > 0.015
      ? "마이크 입력이 확인되었습니다."
      : "입력 소리가 매우 작습니다. 마이크 위치와 장치를 확인해 주세요.";
  } catch (error) {
    dom.micTestStatus.textContent = microphoneErrorMessage(error);
  } finally {
    stream?.getTracks().forEach((track) => track.stop());
    await context?.close().catch(() => {});
    dom.micMeterBar.style.setProperty("--mic-level", "0%");
    dom.micTestBtn.disabled = !dom.consentCheck.checked;
  }
}

function microphoneErrorMessage(error) {
  if (error?.name === "NotAllowedError") return "마이크 권한이 차단되었습니다. 브라우저 주소창의 권한 설정에서 허용해 주세요.";
  if (error?.name === "NotFoundError") return "사용 가능한 마이크를 찾지 못했습니다.";
  if (error?.name === "NotReadableError") return "다른 앱이 마이크를 사용 중이거나 장치를 읽을 수 없습니다.";
  return error?.message || "마이크를 확인하지 못했습니다.";
}

function pickRecorderMimeType() {
  const choices = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"];
  return choices.find((type) => window.MediaRecorder?.isTypeSupported(type)) || "";
}

async function startRecorder() {
  if (!window.MediaRecorder) throw new Error("이 브라우저는 회의 음성 저장을 지원하지 않습니다.");
  if (!state.mediaStream) throw new Error("마이크가 연결되지 않았습니다.");
  if (state.mediaRecorder && state.mediaRecorder.state !== "inactive") return;

  const mimeType = pickRecorderMimeType();
  const recorder = mimeType ? new MediaRecorder(state.mediaStream, { mimeType }) : new MediaRecorder(state.mediaStream);
  state.mediaRecorder = recorder;
  const pending = await listAudioChunks(state.meetingId).catch(() => []);
  const rememberedSequence = Number(localStorage.getItem(audioSequenceKey(state.meetingId)) || 0);
  state.audioSequence = Math.max(rememberedSequence, 0, ...pending.map((item) => Number(item.sequence) || 0));
  state.audioChunkStartedAt = new Date().toISOString();

  recorder.addEventListener("dataavailable", (event) => {
    if (!event.data || event.data.size === 0 || !state.meetingId) return;
    state.audioPersistChain = state.audioPersistChain
      .then(() => persistRecorderChunk(event.data))
      .catch((error) => {
        showBanner("음성 임시 저장을 확인해 주세요", error.message, { tone: "danger", assertive: true });
        flushAudioQueue();
      });
  });
  recorder.addEventListener("error", (event) => {
    showBanner("음성 저장 중 문제가 발생했습니다", event.error?.message || "브라우저 녹음기를 확인해 주세요.", {
      tone: "danger",
      assertive: true,
    });
  });
  recorder.start(AUDIO_TIMESLICE_MS);
}

async function persistRecorderChunk(blob) {
  const endedAt = new Date().toISOString();
  const sequence = ++state.audioSequence;
  localStorage.setItem(audioSequenceKey(state.meetingId), String(sequence));
  const arrayBuffer = await blob.arrayBuffer();
  let sha256 = "";
  if (crypto.subtle) {
    const digest = await crypto.subtle.digest("SHA-256", arrayBuffer);
    sha256 = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
  }
  const record = {
    id: makeId("audio"),
    meetingId: state.meetingId,
    chunk_id: makeId("chunk"),
    sequence,
    blob,
    content_type: blob.type || state.mediaRecorder?.mimeType || "application/octet-stream",
    size_bytes: blob.size,
    sha256,
    started_at: state.audioChunkStartedAt,
    ended_at: endedAt,
    created_at: endedAt,
  };
  state.audioChunkStartedAt = endedAt;
  await saveAudioChunk(record);
  setAudioSyncPending(true);
  flushAudioQueue();
}

async function stopRecorder() {
  const recorder = state.mediaRecorder;
  if (!recorder || recorder.state === "inactive") return;
  await new Promise((resolve) => {
    recorder.addEventListener("stop", resolve, { once: true });
    try {
      if (recorder.state === "recording") recorder.requestData();
      recorder.stop();
    } catch {
      resolve();
    }
  });
  await state.audioPersistChain;
  state.mediaRecorder = null;
}

function pauseRecorder() {
  if (state.mediaRecorder?.state === "recording") state.mediaRecorder.pause();
  state.mediaStream?.getAudioTracks().forEach((track) => { track.enabled = false; });
}

function resumeRecorder() {
  state.mediaStream?.getAudioTracks().forEach((track) => { track.enabled = true; });
  if (state.mediaRecorder?.state === "paused") state.mediaRecorder.resume();
}

function stopMediaTracks() {
  state.mediaStream?.getTracks().forEach((track) => track.stop());
  state.mediaStream = null;
}

function openAudioDb() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("브라우저 임시 저장소를 사용할 수 없습니다."));
      return;
    }
    const request = indexedDB.open(AUDIO_DB_NAME, 1);
    request.addEventListener("upgradeneeded", () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(AUDIO_STORE_NAME)) {
        const store = db.createObjectStore(AUDIO_STORE_NAME, { keyPath: "id" });
        store.createIndex("meetingId", "meetingId", { unique: false });
      }
    });
    request.addEventListener("success", () => resolve(request.result));
    request.addEventListener("error", () => reject(request.error || new Error("임시 저장소를 열 수 없습니다.")));
  });
}

async function audioStoreOperation(mode, operation) {
  const db = await openAudioDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(AUDIO_STORE_NAME, mode);
    const store = transaction.objectStore(AUDIO_STORE_NAME);
    let result;
    try {
      result = operation(store);
    } catch (error) {
      db.close();
      reject(error);
      return;
    }
    transaction.addEventListener("complete", () => {
      db.close();
      resolve(result?.result);
    });
    transaction.addEventListener("error", () => {
      db.close();
      reject(transaction.error || new Error("임시 저장소 작업에 실패했습니다."));
    });
  });
}

async function saveAudioChunk(record) {
  try {
    await audioStoreOperation("readwrite", (store) => store.put(record));
  } catch (error) {
    state.memoryAudioQueue.set(record.id, record);
    if (error?.name === "QuotaExceededError") throw new Error("기기 저장 공간이 부족합니다. 공간을 확보한 뒤 다시 시도해 주세요.");
    throw new Error("기기 임시 저장소를 사용할 수 없습니다. 업로드가 끝날 때까지 이 탭을 닫지 마세요.");
  }
}

async function listAudioChunks(meetingId) {
  let stored = [];
  try {
    stored = await audioStoreOperation("readonly", (store) => store.index("meetingId").getAll(meetingId)) || [];
  } catch {
    stored = [];
  }
  const memory = [...state.memoryAudioQueue.values()].filter((item) => item.meetingId === meetingId);
  return [...stored, ...memory].sort((a, b) => Number(a.sequence) - Number(b.sequence));
}

async function removeAudioChunk(id) {
  state.memoryAudioQueue.delete(id);
  await audioStoreOperation("readwrite", (store) => store.delete(id)).catch(() => {});
}

async function removeMeetingAudio(meetingId) {
  const items = await listAudioChunks(meetingId);
  await Promise.all(items.map((item) => removeAudioChunk(item.id)));
  if (meetingId === state.meetingId) setAudioSyncPending(false);
}

function setAudioSyncPending(pending) {
  const next = Boolean(pending);
  if (state.audioSyncPending === next) return;
  state.audioSyncPending = next;
  if (state.minutes) renderMinutes();
  if (["review", "completed"].includes(state.phase)) setPhase(state.phase, { preserveFocus: true });
}

function reconcileAudioUpload(result) {
  const lifecycle = result?.lifecycle ? normalizePhase(result.lifecycle) : "";
  const requiresRegeneration = Boolean(result?.minutes_stale || result?.regeneration_started || lifecycle === "finalizing");
  if (!requiresRegeneration) return;
  state.minutesStale = Boolean(result?.minutes_stale || state.minutes);
  if (result?.minutes) state.minutes = result.minutes;
  renderMinutes();
  setPhase("finalizing", { focus: true });
  showBanner("새 음성을 회의록에 반영하고 있습니다", "기존 회의록의 저장과 승인은 잠시 중단됩니다. 재생성이 끝나면 다시 검토할 수 있습니다.", {
    context: "minutes-stale",
  });
  renderFinalization();
  startMinutesPolling();
  refreshSnapshot().catch(() => {});
}

async function uploadAudioChunk(record) {
  const uploadMeetingId = record.meetingId;
  if (!uploadMeetingId) throw new Error("음성 청크의 회의 ID가 없습니다.");
  const form = new FormData();
  const extension = record.content_type.includes("mp4") ? "m4a" : "webm";
  form.append("file", record.blob, `${String(record.sequence).padStart(6, "0")}.${extension}`);
  form.append("chunk_id", record.chunk_id);
  form.append("sequence", String(record.sequence));
  form.append("content_type", record.content_type);
  if (record.started_at) form.append("started_at", record.started_at);
  if (record.ended_at) form.append("ended_at", record.ended_at);
  if (record.sha256) form.append("sha256", record.sha256);
  const controller = new AbortController();
  state.audioUploadController = controller;
  try {
    const result = await api(`/api/meetings/${encodeURIComponent(uploadMeetingId)}/audio/chunks`, {
      method: "POST",
      body: form,
      signal: controller.signal,
      timeoutMs: AUDIO_UPLOAD_TIMEOUT_MS,
    });
    if (result && result.persisted === false) throw new Error("서버가 음성 청크를 저장하지 못했습니다.");
    if (uploadMeetingId === state.meetingId) {
      sendMeetingEvent("audio.metadata", { payload: {
          chunk_id: record.chunk_id,
          sequence: record.sequence,
          content_type: record.content_type,
          size_bytes: record.size_bytes,
          sha256: record.sha256 || undefined,
          started_at: record.started_at,
          ended_at: record.ended_at,
        },
      });
    }
    await removeAudioChunk(record.id);
    if (uploadMeetingId === state.meetingId) reconcileAudioUpload(result);
  } finally {
    if (state.audioUploadController === controller) state.audioUploadController = null;
  }
}

function flushAudioQueue() {
  // The browser's internet indicator does not describe same-origin reachability.
  // An offline corporate PC can still upload to localhost.
  if (state.audioFlushPromise || !state.meetingId) return state.audioFlushPromise;
  const meetingId = state.meetingId;
  state.audioFlushPromise = (async () => {
    const items = await listAudioChunks(meetingId);
    if (meetingId === state.meetingId) setAudioSyncPending(items.length > 0);
    for (const item of items) {
      try {
        await uploadAudioChunk(item);
      } catch (error) {
        showBanner("음성을 이 기기에 임시 보관 중입니다", `${items.length}개 청크를 연결 복구 후 업로드합니다.`, {
          actionLabel: "업로드 다시 시도",
          action: flushAudioQueue,
        });
        throw error;
      }
    }
    const remaining = await listAudioChunks(meetingId);
    if (meetingId === state.meetingId) setAudioSyncPending(remaining.length > 0);
    if (meetingId === state.meetingId && items.length && state.socketStatus === "open" && state.realtimeStatus !== "reconnecting" && ["live", "paused"].includes(state.phase)) {
      hideBanner();
    }
  })()
    .catch(() => {})
    .finally(() => { state.audioFlushPromise = null; });
  return state.audioFlushPromise;
}

async function waitForAudioFlush(timeoutMs = FINAL_AUDIO_FLUSH_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remaining = deadline - Date.now();
    const flushed = await Promise.race([
      Promise.resolve(flushAudioQueue()).then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), remaining)),
    ]);
    if (!flushed) {
      state.audioUploadController?.abort();
      return false;
    }
    const pending = await listAudioChunks(state.meetingId);
    if (!pending.length) return true;
    await new Promise((resolve) => setTimeout(resolve, Math.min(500, Math.max(0, deadline - Date.now()))));
  }
  state.audioUploadController?.abort();
  return false;
}

async function connectRealtime() {
  if (!state.meetingId || !state.mediaStream) throw new Error("Realtime 연결 준비가 되지 않았습니다.");
  closeRealtime(true);
  state.realtimeIntentionalClose = false;
  state.realtimeStatus = "connecting";
  renderHealth();

  const tokenResult = await api(meetingPath("/realtime/client-secret"), { method: "POST" });
  const ephemeralKey = tokenResult?.value || tokenResult?.client_secret?.value || tokenResult?.client_secret;
  if (!ephemeralKey || typeof ephemeralKey !== "string") throw new Error("Realtime 단기 연결 정보를 받지 못했습니다.");

  const peer = new RTCPeerConnection();
  const channel = peer.createDataChannel("oai-events");
  state.peer = peer;
  state.realtimeChannel = channel;
  state.mediaStream.getAudioTracks().forEach((track) => peer.addTrack(track, state.mediaStream));

  channel.addEventListener("message", (event) => {
    try {
      handleRealtimeEvent(JSON.parse(event.data));
    } catch (error) {
      showToast(`전사 이벤트를 읽지 못했습니다: ${error.message}`, "danger");
    }
  });
  channel.addEventListener("open", () => {
    state.realtimeStatus = "open";
    state.realtimeRetryAttempt = 0;
    renderHealth();
    sendMeetingEvent("realtime.status", { payload: { status: "connected" } });
  });
  channel.addEventListener("close", () => {
    if (state.realtimeChannel !== channel) return;
    if (!state.realtimeIntentionalClose) scheduleRealtimeReconnect();
  });
  peer.addEventListener("connectionstatechange", () => {
    if (state.peer !== peer) return;
    if (["failed", "disconnected", "closed"].includes(peer.connectionState) && !state.realtimeIntentionalClose) {
      scheduleRealtimeReconnect();
    }
  });

  const offer = await peer.createOffer();
  await peer.setLocalDescription(offer);
  const answerResponse = await fetch("https://api.openai.com/v1/realtime/calls", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${ephemeralKey}`,
      "Content-Type": "application/sdp",
    },
    body: offer.sdp,
  });
  if (!answerResponse.ok) throw new Error(`Realtime 연결에 실패했습니다 (${answerResponse.status}).`);
  const answerSdp = await answerResponse.text();
  await peer.setRemoteDescription({ type: "answer", sdp: answerSdp });
  await waitForChannelOpen(channel, 12_000);
  state.realtimeStatus = "open";
  renderHealth();
}

function waitForChannelOpen(channel, timeoutMs) {
  if (channel.readyState === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Realtime 연결 시간이 초과되었습니다.")), timeoutMs);
    channel.addEventListener("open", () => {
      clearTimeout(timeout);
      resolve();
    }, { once: true });
    channel.addEventListener("close", () => {
      clearTimeout(timeout);
      reject(new Error("Realtime 연결이 완료되기 전에 닫혔습니다."));
    }, { once: true });
  });
}

function closeRealtime(preserveStatus = false) {
  state.realtimeIntentionalClose = true;
  clearTimeout(state.realtimeRetryTimer);
  state.realtimeRetryTimer = null;
  try { state.realtimeChannel?.close(); } catch {}
  try { state.peer?.close(); } catch {}
  state.realtimeChannel = null;
  state.peer = null;
  if (!preserveStatus) state.realtimeStatus = "idle";
  renderHealth();
}

function scheduleRealtimeReconnect() {
  if (state.realtimeIntentionalClose || state.realtimeRetryTimer || !["live", "reconnecting"].includes(state.phase)) return;
  state.realtimeStatus = "reconnecting";
  if (state.phase === "live") {
    state.phaseBeforeReconnect = "live";
    setPhase("reconnecting");
  }
  showBanner("실시간 전사를 다시 연결하고 있습니다", "음성 녹음은 중단되지 않으며 이 기기에 임시 보관됩니다.", {
    actionLabel: "지금 다시 연결",
    action: reconnectAll,
  });
  const index = Math.min(state.realtimeRetryAttempt, WS_RETRY_MS.length - 1);
  state.realtimeRetryTimer = window.setTimeout(async () => {
    state.realtimeRetryTimer = null;
    state.realtimeRetryAttempt += 1;
    try {
      await connectRealtime();
      if (state.socketStatus === "open") {
        hideBanner();
        setPhase("live");
      }
    } catch {
      scheduleRealtimeReconnect();
    }
  }, WS_RETRY_MS[index] + Math.floor(Math.random() * 350));
}

function handleRealtimeEvent(event) {
  const type = event.type || "";
  if (type === "conversation.item.created") {
    const itemId = event.item?.id || event.item_id;
    if (itemId) state.realtimeItemPrevious.set(String(itemId), event.previous_item_id || null);
    return;
  }
  if (type === "input_audio_buffer.committed") {
    if (event.item_id) state.realtimeItemPrevious.set(String(event.item_id), event.previous_item_id || null);
    return;
  }
  if (type === "conversation.item.input_audio_transcription.delta") {
    const item = {
      item_id: event.item_id,
      delta: event.delta || "",
    };
    handlePartialTranscript(item, true);
    return;
  }
  if (type === "conversation.item.input_audio_transcription.completed") {
    handleFinalTranscript({
      item_id: event.item_id,
      previous_item_id: event.previous_item_id || state.realtimeItemPrevious.get(String(event.item_id)) || null,
      text: event.transcript || event.text || "",
      started_at: event.started_at,
      ended_at: event.ended_at || new Date().toISOString(),
    }, true);
    return;
  }
  if (type === "conversation.item.input_audio_transcription.failed" || type === "error") {
    const message = event.error?.message || event.message || "실시간 전사에 실패했습니다.";
    sendMeetingEvent("realtime.status", {
      payload: { status: "error", error_code: event.error?.code || "transcription_error" },
    });
    showBanner("전사 연결을 확인해 주세요", message, {
      tone: "danger",
      assertive: true,
      action: reconnectAll,
    });
    return;
  }
  if (type === "input_audio_buffer.speech_started") {
    dom.captionState.textContent = "발화 감지";
  } else if (type === "input_audio_buffer.speech_stopped") {
    dom.captionState.textContent = "자막 확정 중";
  }
}

function handlePartialTranscript(item, fromRealtime) {
  const id = String(item.item_id || item.id || "active");
  if (state.finalizedIds.has(id)) return;
  const previous = state.partials.get(id) || "";
  const text = item.text != null ? String(item.text) : `${previous}${item.delta || ""}`;
  state.partials.set(id, text);
  dom.captionPartial.textContent = text;
  if (fromRealtime) {
    sendMeetingEvent("transcript.partial", {
      item_id: id,
      delta: item.delta || "",
      text,
      client_event_id: makeId("partial"),
    }, { queue: false });
  }
}

function normalizeTranscript(item, index = 0) {
  const id = String(item.item_id || item.external_item_id || item.id || item.utterance_id || item.index || `utterance-${index}`);
  return {
    ...item,
    id,
    item_id: id,
    text: String(item.text || item.transcript || "").trim(),
    speaker_id: item.speaker_id || item.speaker || "speaker",
    speaker: item.speaker_label || item.speaker_name || item.speaker || "화자",
    started_at: item.started_at || item.timestamp || item.occurred_at || "",
    ended_at: item.ended_at || item.timestamp || item.occurred_at || "",
    status: item.status || "final",
    previous_item_id: item.previous_item_id || item.previous_id || null,
    _arrival: Number(item._arrival ?? index),
  };
}

function orderTranscript(items) {
  const byId = new Map(items.map((item) => [item.id, item]));
  const children = new Map();
  const roots = [];
  items.forEach((item) => {
    const previous = item.previous_item_id;
    if (previous && byId.has(String(previous))) {
      const key = String(previous);
      if (!children.has(key)) children.set(key, []);
      children.get(key).push(item);
    } else {
      roots.push(item);
    }
  });
  const compare = (left, right) => {
    const leftTime = new Date(left.started_at || left.ended_at || 0).getTime();
    const rightTime = new Date(right.started_at || right.ended_at || 0).getTime();
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) return leftTime - rightTime;
    return left._arrival - right._arrival;
  };
  roots.sort(compare);
  children.forEach((list) => list.sort(compare));
  const ordered = [];
  const visited = new Set();
  const visit = (item) => {
    if (!item || visited.has(item.id)) return;
    visited.add(item.id);
    ordered.push(item);
    (children.get(item.id) || []).forEach(visit);
  };
  roots.forEach(visit);
  items.slice().sort(compare).forEach(visit);
  return ordered;
}

function replaceTranscript(items) {
  state.transcript = [];
  state.finalizedIds.clear();
  items.forEach((item, index) => {
    const normalized = normalizeTranscript({ ...item, _arrival: index }, index);
    if (!normalized.text || state.finalizedIds.has(normalized.id)) return;
    state.finalizedIds.add(normalized.id);
    state.transcript.push(normalized);
  });
  state.transcript = orderTranscript(state.transcript);
  state.transcriptArrivalSeq = state.transcript.length;
  renderTranscript(false);
}

function handleFinalTranscript(item, fromRealtime) {
  const normalized = normalizeTranscript({ ...item, _arrival: ++state.transcriptArrivalSeq }, state.transcript.length);
  if (!normalized.text || state.finalizedIds.has(normalized.id)) return;
  state.finalizedIds.add(normalized.id);
  state.partials.delete(normalized.id);
  if (state.partials.size) {
    const latest = [...state.partials.values()].at(-1);
    dom.captionPartial.textContent = latest || "";
  } else {
    dom.captionPartial.textContent = "";
  }
  state.transcript.push(normalized);
  state.transcript = orderTranscript(state.transcript);
  dom.captionAnnouncement.textContent = `${displaySpeaker(normalized)}: ${normalized.text}`;
  renderTranscript(false);
  if (fromRealtime) {
    sendMeetingEvent("transcript.final", {
      item_id: normalized.id,
      previous_item_id: item.previous_item_id,
      text: normalized.text,
      speaker: normalized.speaker_id,
      started_at: normalized.started_at || undefined,
      ended_at: normalized.ended_at || new Date().toISOString(),
      client_event_id: makeId("final"),
    });
  }
}

function renderTranscript(fullReplace = false) {
  const latest = state.transcript.slice(-2);
  dom.captionFinals.replaceChildren();
  if (!latest.length) {
    const empty = document.createElement("li");
    empty.className = "empty-line";
    empty.textContent = "확정된 발화를 기다리고 있습니다.";
    dom.captionFinals.appendChild(empty);
  } else {
    latest.forEach((item) => {
      const li = document.createElement("li");
      const speaker = document.createElement("span");
      speaker.className = "caption-speaker";
      speaker.textContent = displaySpeaker(item);
      const text = document.createElement("span");
      text.textContent = item.text;
      li.append(speaker, text);
      dom.captionFinals.appendChild(li);
    });
  }

  const wasNearBottom = isNearBottom(dom.transcriptList);
  const previousScrollTop = dom.transcriptList.scrollTop;
  const incomingIds = new Set(state.transcript.map((item) => item.id));
  const added = [...incomingIds].filter((id) => !state.lastTranscriptIds.has(id)).length;
  if (fullReplace) dom.transcriptList.replaceChildren();
  if (state.transcript.length) $(".empty-row", dom.transcriptList)?.remove();
  state.transcript.forEach((item) => upsertTranscriptNode(item));
  state.transcript.forEach((item) => {
    const node = $(`[data-transcript-id="${CSS.escape(item.id)}"]`, dom.transcriptList);
    if (node) dom.transcriptList.appendChild(node);
  });
  $$("li[data-transcript-id]", dom.transcriptList).forEach((node) => {
    if (!incomingIds.has(node.dataset.transcriptId)) node.remove();
  });
  if (!state.transcript.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "확정된 자막이 없습니다.";
    dom.transcriptList.replaceChildren(empty);
  }
  if (added && !wasNearBottom && dom.transcriptDrawer.getAttribute("aria-hidden") === "false") {
    state.unreadTranscript += added;
    dom.newTranscriptBtn.textContent = `새 자막 ${state.unreadTranscript}개`;
    dom.newTranscriptBtn.hidden = false;
    dom.transcriptList.scrollTop = previousScrollTop;
  } else if (!wasNearBottom && dom.transcriptDrawer.getAttribute("aria-hidden") === "false") {
    dom.transcriptList.scrollTop = previousScrollTop;
  } else if (wasNearBottom && dom.transcriptDrawer.getAttribute("aria-hidden") === "false") {
    dom.transcriptList.scrollTop = dom.transcriptList.scrollHeight;
  }
  state.lastTranscriptIds = incomingIds;
}

function upsertTranscriptNode(item) {
  let node = $(`[data-transcript-id="${CSS.escape(item.id)}"]`, dom.transcriptList);
  if (!node) {
    node = document.createElement("li");
    node.dataset.transcriptId = item.id;
    node.id = `utterance-${item.id}`;
    node.append(document.createElement("time"), document.createElement("span"), document.createElement("p"));
    dom.transcriptList.appendChild(node);
  }
  const time = $("time", node);
  time.dateTime = item.started_at || "";
  time.textContent = formatLocalTime(item.started_at || item.ended_at);
  const speaker = $("span", node);
  speaker.className = "transcript-speaker";
  speaker.textContent = ` · ${displaySpeaker(item)}`;
  $("p", node).textContent = item.text;
}

function displaySpeaker(item) {
  const key = item.speaker_id || item.speaker || "speaker";
  return state.speakerNames[key] || item.speaker_name || item.speaker_label || item.speaker || "화자";
}

function renderMeetingData() {
  const topic = getText(state.meeting?.topic || state.currentTopic, "새 회의");
  dom.meetingTitleHeader.textContent = topic;
  document.title = `${topic} · good-listener`;
  renderTranscript(false);
  renderProgress();
  renderFacts(false);
  renderLedger();
  renderHealth();
  updateTimer();
}

function renderProgress() {
  const progress = state.progress || {};
  const topic = getText(progress.current_topic || state.currentTopic || state.meeting?.topic, "논의가 시작되면 현재 안건을 표시합니다.");
  const summary = getText(progress.summary || progress.current_summary || progress.overview);
  dom.currentTopic.textContent = topic;
  if (summary) dom.currentSummary.textContent = summary;
  else {
    const items = safeArray(progress.agenda_items || progress.items || progress.agenda);
    const discussing = items.filter((item) => ["discussing", "current"].includes(String(item.status))).map(getText).filter(Boolean);
    const decided = items.filter((item) => ["decided", "completed"].includes(String(item.status))).length;
    if (discussing.length) dom.currentSummary.textContent = `${discussing.slice(0, 2).join(" · ")} 논의 중`;
    else if (decided) dom.currentSummary.textContent = `${decided}개 안건이 결정되었습니다.`;
    else if (!dom.currentSummary.textContent.trim()) dom.currentSummary.textContent = "아직 확정된 요약이 없습니다.";
  }
  dom.progressUpdating.hidden = !Boolean(progress.updating || progress.status === "processing");
  dom.progressUpdatedAt.textContent = formatRelativeTime(progress.updated_at || progress.updatedAt);

  const agenda = safeArray(progress.agenda_items || progress.items || progress.agenda);
  dom.agendaList.replaceChildren();
  if (!agenda.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "진행 상태를 정리하고 있습니다.";
    dom.agendaList.appendChild(empty);
  } else {
    agenda.slice(0, 8).forEach((item, index) => {
      const li = document.createElement("li");
      const rawStatus = String(item.status || (item.completed ? "completed" : index === 0 ? "current" : "pending"));
      const status = rawStatus === "discussing" ? "current" : rawStatus === "decided" ? "completed" : rawStatus;
      li.className = "agenda-item";
      li.dataset.status = status;
      const icon = document.createElement("span");
      icon.className = "agenda-status-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = status === "completed" ? "✓" : status === "current" ? "•" : String(index + 1);
      const text = document.createElement("strong");
      text.textContent = getText(item, `안건 ${index + 1}`);
      const label = document.createElement("span");
      label.textContent = status === "completed" ? "완료" : status === "current" ? "논의 중" : status === "blocked" ? "막힘" : "대기";
      li.append(icon, text, label);
      dom.agendaList.appendChild(li);
    });
  }
  const evidence = safeArray(progress.evidence_utterance_ids || progress.utterance_ids);
  dom.progressEvidenceBtn.hidden = !evidence.length;
  dom.progressEvidenceBtn.dataset.evidenceIds = evidence.join(",");
}

function normalizeFact(fact, index = 0) {
  const rawStatus = String(fact.status || fact.verdict_status || fact.result || "queued").toLowerCase();
  let status = rawStatus;
  if (["pending", "processing", "in_progress"].includes(status)) status = "searching";
  if (["true", "confirmed", "supported", "correct"].includes(status)) status = "verified";
  if (["false", "refuted", "incorrect"].includes(status)) status = "contradicted";
  if (["unknown", "uncertain", "needs_review", "insufficient", "internal_source_required"].includes(status)) status = "inconclusive";
  if (!["queued", "searching", "verified", "contradicted", "inconclusive", "failed"].includes(status)) status = "inconclusive";
  return {
    ...fact,
    id: String(fact.claim_id || fact.id || `fact-${index}`),
    status,
    claim: getText(fact.claim || fact.claim_text || fact.statement || fact.text, "검증 중인 주장"),
    verdict: getText(fact.verdict || fact.explanation || fact.summary || fact.result_text),
    sources: safeArray(fact.sources || fact.citations),
  };
}

function upsertFact(fact) {
  const normalized = normalizeFact(fact, state.facts.length);
  const index = state.facts.findIndex((item) => String(item.claim_id || item.id) === normalized.id);
  if (index >= 0) state.facts[index] = { ...state.facts[index], ...fact };
  else state.facts.push(fact);
  renderFacts(false);
}

function factStatusLabel(status) {
  return {
    queued: "대기",
    searching: "검색 중",
    verified: "확인",
    contradicted: "반박",
    inconclusive: "추가 확인",
    failed: "검색 실패",
  }[status] || "추가 확인";
}

function renderFacts(fullReplace = false) {
  const facts = state.facts.map(normalizeFact);
  const wasNearBottom = isNearBottom(dom.factViewport);
  const previousScrollTop = dom.factViewport.scrollTop;
  const ids = new Set(facts.map((fact) => fact.id));
  const added = [...ids].filter((id) => !state.lastFactIds.has(id)).length;
  if (fullReplace) dom.factList.replaceChildren();
  if (facts.length) $(".empty-row", dom.factList)?.remove();
  facts.forEach((fact) => upsertFactNode(fact));
  facts.forEach((fact) => {
    const node = $(`[data-fact-id="${CSS.escape(fact.id)}"]`, dom.factList);
    if (node) dom.factList.appendChild(node);
  });
  $$("li[data-fact-id]", dom.factList).forEach((node) => {
    if (!ids.has(node.dataset.factId)) node.remove();
  });
  if (!facts.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "검증할 주장을 기다리고 있습니다.";
    dom.factList.replaceChildren(empty);
  }

  const pending = facts.filter((fact) => ["queued", "searching"].includes(fact.status)).length;
  dom.factPendingCount.textContent = `대기 ${pending}`;
  dom.factVerifiedCount.textContent = String(facts.filter((fact) => fact.status === "verified").length);
  dom.factContradictedCount.textContent = String(facts.filter((fact) => fact.status === "contradicted").length);
  dom.factInconclusiveCount.textContent = String(facts.filter((fact) => ["inconclusive", "failed"].includes(fact.status)).length);

  if (added && !wasNearBottom) {
    state.unreadFacts += added;
    dom.newFactsBtn.textContent = `새 팩트 ${state.unreadFacts}개`;
    dom.newFactsBtn.hidden = false;
    dom.factViewport.scrollTop = previousScrollTop;
  } else if (!wasNearBottom) {
    dom.factViewport.scrollTop = previousScrollTop;
  } else if (wasNearBottom) {
    dom.factViewport.scrollTop = dom.factViewport.scrollHeight;
  }
  state.lastFactIds = ids;
}

function upsertFactNode(fact) {
  let node = $(`[data-fact-id="${CSS.escape(fact.id)}"]`, dom.factList);
  if (!node) {
    node = document.createElement("li");
    node.className = "fact-item";
    node.dataset.factId = fact.id;
    const status = document.createElement("span");
    status.className = "fact-status";
    const copy = document.createElement("div");
    copy.className = "fact-copy";
    copy.append(document.createElement("strong"), document.createElement("p"), document.createElement("div"));
    $("div", copy).className = "fact-sources";
    node.append(status, copy);
    dom.factList.appendChild(node);
  }
  node.dataset.status = fact.status;
  $(".fact-status", node).textContent = factStatusLabel(fact.status);
  $("strong", node).textContent = fact.claim;
  const verdict = $("p", node);
  verdict.textContent = fact.verdict || (fact.status === "searching" ? "출처를 찾고 있습니다. 발화 수집은 계속됩니다." : "결과를 기다리고 있습니다.");
  const sources = $(".fact-sources", node);
  sources.replaceChildren();
  fact.sources.slice(0, 3).forEach((source, index) => {
    const url = source.url || source.href;
    if (!isSafeUrl(url)) return;
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = source.title || source.domain || `출처 ${index + 1}`;
    sources.appendChild(link);
  });
}

function renderLedger() {
  dom.decisionCount.textContent = String(state.decisions.length);
  dom.actionCount.textContent = String(state.actions.length);
  dom.openQuestionCount.textContent = String(state.openQuestions.length);
  dom.suggestionCount.textContent = String(state.suggestions.length);
  if (state.activeDrawer) renderDrawer(state.activeDrawer);
}

const drawerConfig = {
  decisions: { title: "결정", items: () => state.decisions },
  actions: { title: "할 일", items: () => state.actions },
  open_questions: { title: "미결 사항", items: () => state.openQuestions },
  suggestions: { title: "AI 제안", items: () => state.suggestions },
};

function openDrawer(type, opener) {
  if (!drawerConfig[type]) return;
  state.activeDrawer = type;
  state.drawerOpener = opener || document.activeElement;
  dom.insightDrawer.setAttribute("aria-hidden", "false");
  setBackgroundInert(true);
  $$('[data-drawer]').forEach((button) => button.setAttribute("aria-expanded", String(button.dataset.drawer === type)));
  renderDrawer(type);
  $(".drawer-sheet", dom.insightDrawer).focus();
}

function renderDrawer(type) {
  const config = drawerConfig[type];
  if (!config) return;
  dom.drawerTitle.textContent = config.title;
  dom.drawerList.replaceChildren();
  const items = safeArray(config.items());
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = `${config.title}이 아직 없습니다.`;
    dom.drawerList.appendChild(empty);
    return;
  }
  items.forEach((item, index) => {
    const li = document.createElement("li");
    li.className = "drawer-item";
    const title = document.createElement("strong");
    title.textContent = getText(item, `${config.title} ${index + 1}`);
    li.appendChild(title);
    const metadata = drawerMetadata(type, item);
    if (metadata) {
      const detail = document.createElement("p");
      detail.textContent = metadata;
      li.appendChild(detail);
    }
    const evidenceIds = safeArray(item.evidence_utterance_ids || item.utterance_ids || item.evidence_ids);
    if (evidenceIds.length) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "text-button evidence-link";
      button.textContent = "근거 발화 보기";
      button.addEventListener("click", () => openTranscriptAt(evidenceIds[0]));
      li.appendChild(button);
    }
    dom.drawerList.appendChild(li);
  });
}

function drawerMetadata(type, item) {
  if (type === "actions") {
    const owner = item.owner || item.assignee || "담당자 미정";
    const due = item.due_at || item.due_date || item.deadline || "기한 미정";
    return `${owner} · ${due}`;
  }
  return getText(item.detail || item.reason || item.rationale || item.status);
}

function closeDrawer() {
  if (dom.insightDrawer.getAttribute("aria-hidden") === "true") return;
  dom.insightDrawer.setAttribute("aria-hidden", "true");
  if (dom.transcriptDrawer.getAttribute("aria-hidden") === "true") setBackgroundInert(false);
  $$('[data-drawer]').forEach((button) => button.setAttribute("aria-expanded", "false"));
  state.activeDrawer = null;
  state.drawerOpener?.focus?.();
}

function openTranscript(opener) {
  state.transcriptOpener = opener || document.activeElement;
  dom.transcriptDrawer.setAttribute("aria-hidden", "false");
  setBackgroundInert(true);
  state.unreadTranscript = 0;
  dom.newTranscriptBtn.hidden = true;
  $(".drawer-sheet", dom.transcriptDrawer).focus();
}

function closeTranscript() {
  if (dom.transcriptDrawer.getAttribute("aria-hidden") === "true") return;
  dom.transcriptDrawer.setAttribute("aria-hidden", "true");
  if (dom.insightDrawer.getAttribute("aria-hidden") === "true") setBackgroundInert(false);
  state.transcriptOpener?.focus?.();
}

function setBackgroundInert(value) {
  [$(".app-header"), dom.systemBanner, $("#mainContent")].forEach((node) => {
    if (node) node.inert = value;
  });
}

function openTranscriptAt(id) {
  closeDrawer();
  openTranscript();
  requestAnimationFrame(() => {
    const node = $(`[data-transcript-id="${CSS.escape(String(id))}"]`, dom.transcriptList);
    if (!node) return;
    $$("[data-highlighted]", dom.transcriptList).forEach((item) => delete item.dataset.highlighted);
    node.dataset.highlighted = "true";
    node.scrollIntoView({ block: "center" });
    node.tabIndex = -1;
    node.focus({ preventScroll: true });
  });
}

function trapDrawerFocus(event, drawer) {
  if (event.key !== "Tab" || drawer.getAttribute("aria-hidden") === "true") return;
  const focusable = $$('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])', drawer);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

async function createAndStartMeeting() {
  if (state.busy) return;
  if (!dom.setupForm.reportValidity()) return;
  state.busy = true;
  setPhase("connecting");
  showBanner("회의를 연결하고 있습니다", "마이크, 실시간 전사와 안전한 음성 저장소를 준비합니다.");
  try {
    await requestMicrophone();
    const created = await api("/api/meetings", {
      method: "POST",
      body: JSON.stringify({
        topic: dom.meetingTopic.value.trim(),
        goal: dom.meetingGoal.value.trim(),
        terms: parseTerms(dom.meetingTerms.value),
        consent_external_processing: true,
      }),
    });
    applySnapshot(created, { keepConnecting: true });
    const meeting = created?.meeting || created;
    const id = meeting?.id || created?.meeting_id;
    if (!id) throw new Error("서버가 회의 ID를 반환하지 않았습니다.");
    rememberMeeting(String(id));
    state.meeting = { ...(state.meeting || {}), ...meeting };
    state.startedAt = meeting.started_at || new Date().toISOString();
    dom.meetingTitleHeader.textContent = meeting.topic || dom.meetingTopic.value.trim();

    await connectMeetingSocket();
    const started = await api(meetingPath("/start"), { method: "POST" });
    if (started) applySnapshot(started, { keepConnecting: true });
    await startRecorder();
    await connectRealtime();
    setPhase("live", { focus: true });
    hideBanner();
  } catch (error) {
    await stopRecorder().catch(() => {});
    closeRealtime();
    if (!state.meetingId) stopMediaTracks();
    setPhase("error");
    showBanner("회의를 시작하지 못했습니다", microphoneErrorMessage(error), {
      tone: "danger",
      assertive: true,
      actionLabel: state.meetingId ? "다시 연결" : "다시 시도",
      action: state.meetingId ? reconnectAll : createAndStartMeeting,
    });
  } finally {
    state.busy = false;
    setPhase(state.phase, { preserveFocus: true });
  }
}

async function reconnectAll() {
  if (!state.meetingId || state.busy) return;
  if (["setup", "connecting", "finalizing", "review", "completed"].includes(state.phase)) {
    connectMeetingSocket().catch(scheduleSocketReconnect);
    flushAudioQueue();
    return;
  }
  if (state.phase === "error" && ["failed", "finalizing"].includes(String(state.meeting?.lifecycle))) {
    await retryFinalization();
    return;
  }
  state.busy = true;
  if (state.phase !== "reconnecting") state.phaseBeforeReconnect = state.phase === "paused" ? "paused" : "live";
  setPhase("reconnecting");
  try {
    if (!state.mediaStream?.getAudioTracks().some((track) => track.readyState === "live")) {
      stopMediaTracks();
      await requestMicrophone();
    }
    await connectMeetingSocket();
    await connectRealtime();
    if (!state.mediaRecorder || state.mediaRecorder.state === "inactive") await startRecorder();
    if (state.phaseBeforeReconnect === "paused") {
      pauseRecorder();
      setPhase("paused");
    } else {
      resumeRecorder();
      setPhase("live");
    }
    hideBanner();
    flushAudioQueue();
  } catch (error) {
    setPhase("error");
    showBanner("연결을 복구하지 못했습니다", error.message, {
      tone: "danger",
      assertive: true,
      actionLabel: "다시 연결",
      action: reconnectAll,
    });
  } finally {
    state.busy = false;
    setPhase(state.phase, { preserveFocus: true });
  }
}

async function togglePause() {
  if (!state.meetingId || state.busy) return;
  state.busy = true;
  const wasPaused = state.phase === "paused";
  try {
    if (wasPaused) {
      const result = await api(meetingPath("/resume"), { method: "POST" });
      resumeRecorder();
      if (state.realtimeStatus !== "open") await connectRealtime();
      if (result) applySnapshot(result);
      setPhase("live");
      hideBanner();
    } else {
      pauseRecorder();
      const result = await api(meetingPath("/pause"), { method: "POST" });
      if (result) applySnapshot(result);
      state.partials.clear();
      dom.captionPartial.textContent = "";
      setPhase("paused");
      showBanner("회의가 일시정지되었습니다", "마이크 음성은 전사하거나 저장하지 않습니다.");
    }
  } catch (error) {
    if (wasPaused) pauseRecorder();
    else resumeRecorder();
    showBanner("회의 상태를 변경하지 못했습니다", error.message, { tone: "danger", assertive: true });
  } finally {
    state.busy = false;
    setPhase(state.phase, { preserveFocus: true });
  }
}

async function finalizeMeeting() {
  if (!state.meetingId || state.busy) return;
  state.busy = true;
  setPhase("finalizing", { focus: true });
  hideBanner();
  try {
    state.mediaStream?.getAudioTracks().forEach((track) => { track.enabled = false; });
    await stopRecorder();
    closeRealtime();
    stopMediaTracks();
    renderFinalization();
    const flushed = await waitForAudioFlush();
    if (!flushed) {
      showBanner("일부 음성을 이 기기에 보관 중입니다", "네트워크가 복구되면 남은 청크를 업로드합니다. 회의록 작성은 계속됩니다.");
    }
    const result = await api(meetingPath("/stop"), { method: "POST" });
    if (result) applySnapshot(result);
    setPhase("finalizing");
    renderFinalization();
    startMinutesPolling();
  } catch (error) {
    setPhase("error");
    showBanner("회의 마무리를 완료하지 못했습니다", "저장된 자막과 음성으로 다시 시도할 수 있습니다.", {
      tone: "danger",
      assertive: true,
      actionLabel: "마무리 다시 시도",
      action: finalizeMeeting,
    });
  } finally {
    state.busy = false;
    setPhase(state.phase, { preserveFocus: true });
  }
}

async function retryFinalization() {
  if (!state.meetingId || state.busy) return;
  state.busy = true;
  setPhase("finalizing", { focus: true });
  hideBanner();
  try {
    const result = await api(meetingPath("/retry-finalization"), { method: "POST" });
    if (result) applySnapshot(result);
    setPhase("finalizing");
    startMinutesPolling();
  } catch (error) {
    setPhase("error");
    showBanner("회의록을 다시 만들지 못했습니다", error.message, {
      tone: "danger",
      assertive: true,
      actionLabel: "다시 시도",
      action: retryFinalization,
    });
  } finally {
    state.busy = false;
    setPhase(state.phase, { preserveFocus: true });
  }
}

function renderFinalization() {
  const jobs = state.pendingJobs || {};
  const genericPending = typeof jobs === "number" ? jobs : null;
  const activeFacts = state.facts.map(normalizeFact).some((fact) => ["queued", "searching"].includes(fact.status));
  const minutesCurrent = Boolean(state.minutes) && !state.minutesStale && String(state.minutes?.status || "").toLowerCase() !== "stale";
  const steps = genericPending == null
    ? {
        audio: !state.mediaRecorder,
        transcript: !jobPending(jobs, ["transcript", "transcription", "stt"]),
        facts: !jobPending(jobs, ["facts", "fact_checks", "fact_check"]),
        minutes: minutesCurrent || !jobPending(jobs, ["minutes", "report"]),
      }
    : {
        audio: !state.mediaRecorder,
        transcript: !state.mediaRecorder,
        facts: !activeFacts && genericPending === 0,
        minutes: minutesCurrent,
      };
  const firstIncomplete = Object.keys(steps).find((key) => !steps[key]);
  $$('[data-step]', dom.finalizationSteps).forEach((node) => {
    const key = node.dataset.step;
    node.dataset.status = steps[key] ? "complete" : key === firstIncomplete ? "active" : "pending";
  });
  dom.finalizingMessage.textContent = firstIncomplete === "audio"
    ? "마지막 음성을 안전하게 저장하고 있습니다."
    : firstIncomplete === "transcript"
      ? "중간 자막을 확정하고 발화 순서를 정리합니다."
      : firstIncomplete === "facts"
        ? "진행 중인 팩트 검색을 마무리합니다."
        : "결정, 할 일과 미결 사항을 회의록으로 정리합니다.";
}

function jobPending(jobs, keys) {
  for (const key of keys) {
    const value = jobs?.[key];
    if (value == null) continue;
    if (typeof value === "number") return value > 0;
    if (typeof value === "boolean") return value;
    const status = String(value.status || value.state || "").toLowerCase();
    const count = Number(value.pending ?? value.count ?? 0);
    return count > 0 || !["complete", "completed", "done", "failed"].includes(status);
  }
  return true;
}

function startMinutesPolling() {
  if (state.minutesPollTimer || !state.meetingId) return;
  state.minutesPollTimer = window.setInterval(async () => {
    try {
      const snapshot = await api(meetingPath());
      applySnapshot(snapshot);
      if (state.minutes && !state.minutesStale && ["review", "completed"].includes(state.phase)) stopMinutesPolling();
    } catch {
      // WebSocket reconnect and the next poll provide recovery.
    }
  }, 3_000);
}

function stopMinutesPolling() {
  clearInterval(state.minutesPollTimer);
  state.minutesPollTimer = null;
}

function buildMinutesMarkdown() {
  const title = state.meeting?.topic || "회의록";
  const lines = [`# ${title}`, ""];
  const overview = getText(state.minutes?.structured?.overview || state.progress?.summary || state.progress?.current_summary);
  if (overview) lines.push("## 요약", "", overview, "");
  appendMarkdownSection(lines, "결정 사항", state.decisions);
  appendMarkdownSection(lines, "할 일", state.actions, (item) => {
    const owner = item.owner || item.assignee || "담당자 미정";
    const due = item.due_at || item.due_date || item.deadline || "기한 미정";
    return `${getText(item)} — ${owner}, ${due}`;
  });
  appendMarkdownSection(lines, "팩트 확인", state.facts, (item) => {
    const fact = normalizeFact(item);
    return `[${factStatusLabel(fact.status)}] ${fact.claim}${fact.verdict ? ` — ${fact.verdict}` : ""}`;
  });
  appendMarkdownSection(lines, "미결 사항", state.openQuestions);
  return lines.join("\n").trim() + "\n";
}

function appendMarkdownSection(lines, title, items, render = getText) {
  lines.push(`## ${title}`, "");
  if (!items.length) lines.push("- 없음", "");
  else {
    items.forEach((item) => lines.push(`- ${render(item)}`));
    lines.push("");
  }
}

function renderMinutes() {
  const minutes = state.minutes || {};
  const status = String(minutes.status || "draft").toLowerCase();
  const approved = ["approved", "completed", "final"].includes(status);
  const stale = state.minutesStale || status === "stale" || state.phase === "finalizing";
  const waitingForAudio = state.audioSyncPending;
  const locked = approved || stale || waitingForAudio;
  const markdown = minutes.markdown || buildMinutesMarkdown();
  dom.minutesTitle.textContent = state.meeting?.topic || "회의록 초안";
  dom.minutesPhaseLabel.textContent = stale
    ? "최신 음성 반영 중"
    : waitingForAudio
      ? "남은 음성 업로드 중"
      : approved
        ? "확정된 회의록"
        : "회의록 검토";
  dom.minutesMeta.textContent = `${formatLocalTime(minutes.generated_at || state.endedAt || new Date())} · ${state.transcript.length}개 확정 발화`;
  if (document.activeElement !== dom.minutesMarkdown) dom.minutesMarkdown.value = markdown;
  dom.minutesPrintContent.textContent = dom.minutesMarkdown.value;
  dom.minutesMarkdown.readOnly = locked;
  dom.saveMinutesBtn.hidden = approved;
  dom.approveMinutesBtn.hidden = approved;
  dom.saveMinutesBtn.disabled = stale || waitingForAudio;
  dom.approveMinutesBtn.disabled = stale || waitingForAudio;
  const lockReason = stale
    ? "최신 음성을 반영한 회의록을 다시 만드는 중입니다."
    : waitingForAudio
      ? "남은 음성 업로드가 끝난 뒤 검토할 수 있습니다."
      : "";
  dom.saveMinutesBtn.title = lockReason;
  dom.approveMinutesBtn.title = lockReason;
  renderSpeakers(locked);
  renderReviewIssues();
}

function collectSpeakers() {
  const speakers = new Map();
  state.transcript.forEach((item) => {
    const id = item.speaker_id || item.speaker || "speaker";
    if (!speakers.has(id)) speakers.set(id, displaySpeaker(item));
  });
  const structured = state.minutes?.structured || {};
  const names = structured.speaker_names || structured.speakers || {};
  if (Array.isArray(names)) {
    names.forEach((speaker) => speakers.set(speaker.id || speaker.speaker_id, speaker.name || speaker.label));
  } else if (names && typeof names === "object") {
    Object.entries(names).forEach(([id, name]) => speakers.set(id, String(name)));
  }
  return speakers;
}

function renderSpeakers(readOnly) {
  const speakers = collectSpeakers();
  dom.speakerCount.textContent = `${speakers.size}명`;
  dom.speakerList.replaceChildren();
  if (!speakers.size) {
    const empty = document.createElement("p");
    empty.className = "empty-row";
    empty.textContent = "화자 정보가 없습니다.";
    dom.speakerList.appendChild(empty);
    return;
  }
  speakers.forEach((name, id) => {
    if (!(id in state.speakerNames)) state.speakerNames[id] = name;
    const row = document.createElement("div");
    row.className = "speaker-row";
    const label = document.createElement("label");
    label.htmlFor = `speaker-${id}`;
    label.textContent = id;
    const input = document.createElement("input");
    input.id = `speaker-${id}`;
    input.value = state.speakerNames[id] || name;
    input.maxLength = 80;
    input.readOnly = readOnly;
    input.addEventListener("input", () => { state.speakerNames[id] = input.value.trim() || name; });
    row.append(label, input);
    dom.speakerList.appendChild(row);
  });
}

function reviewIssues() {
  const structured = state.minutes?.structured || {};
  const issues = safeArray(structured.review_required || structured.uncertain_items || structured.review_issues || structured.warnings).map(getText).filter(Boolean);
  const minuteFacts = safeArray(structured.facts).length ? structured.facts : state.facts;
  const minuteActions = safeArray(structured.action_items).length ? structured.action_items : state.actions;
  minuteFacts.map(normalizeFact).filter((fact) => ["inconclusive", "failed"].includes(fact.status)).forEach((fact) => {
    issues.push(`팩트 추가 확인: ${fact.claim}`);
  });
  minuteActions.forEach((item) => {
    if (!item.owner && !item.assignee) issues.push(`담당자 미정: ${getText(item)}`);
    if (!item.due_at && !item.due_date && !item.deadline) issues.push(`기한 미정: ${getText(item)}`);
  });
  return [...new Set(issues)];
}

function renderReviewIssues() {
  const issues = reviewIssues();
  dom.reviewIssueCount.textContent = `${issues.length}건`;
  dom.reviewIssueList.replaceChildren();
  if (!issues.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "확인이 필요한 항목이 없습니다.";
    dom.reviewIssueList.appendChild(empty);
  } else {
    issues.forEach((issue) => {
      const li = document.createElement("li");
      li.textContent = issue;
      dom.reviewIssueList.appendChild(li);
    });
  }
}

async function saveMinutes() {
  if (!state.meetingId || state.busy) return false;
  const pendingAudio = await listAudioChunks(state.meetingId).catch(() => []);
  setAudioSyncPending(pendingAudio.length > 0);
  if (state.phase !== "review" || state.minutesStale || pendingAudio.length || state.audioFlushPromise) {
    showToast("최신 음성 반영이 끝난 뒤 회의록을 저장할 수 있습니다.", "danger");
    renderMinutes();
    return false;
  }
  state.busy = true;
  dom.saveMinutesBtn.disabled = true;
  try {
    const structured = {
      ...(state.minutes?.structured || {}),
      speaker_names: { ...state.speakerNames },
    };
    const result = await api(meetingPath("/minutes"), {
      method: "PATCH",
      body: JSON.stringify({ markdown: dom.minutesMarkdown.value, structured }),
    });
    state.minutes = result?.minutes || result || { ...state.minutes, markdown: dom.minutesMarkdown.value, structured };
    renderMinutes();
    renderTranscript(true);
    showToast("회의록 수정을 저장했습니다.");
    return true;
  } catch (error) {
    showToast(`저장하지 못했습니다: ${error.message}`, "danger");
    return false;
  } finally {
    state.busy = false;
    renderMinutes();
  }
}

async function approveMinutes() {
  if (!state.meetingId || state.busy) return;
  if (state.phase !== "review" || state.minutesStale || state.audioSyncPending || state.audioFlushPromise) {
    showToast("최신 음성 반영이 끝난 뒤 회의록을 확정할 수 있습니다.", "danger");
    renderMinutes();
    return;
  }
  const saved = await saveMinutes();
  if (!saved) return;
  state.busy = true;
  dom.approveMinutesBtn.disabled = true;
  try {
    const result = await api(meetingPath("/minutes/approve"), { method: "POST" });
    state.minutes = result?.minutes || result || { ...state.minutes, status: "approved" };
    setPhase("completed", { focus: true });
    renderMinutes();
    showToast("회의록을 확정했습니다.");
  } catch (error) {
    showToast(`확정하지 못했습니다: ${error.message}`, "danger");
  } finally {
    state.busy = false;
    renderMinutes();
  }
}

function downloadMinutes() {
  const markdown = dom.minutesMarkdown.value || state.minutes?.markdown || buildMinutesMarkdown();
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${sanitizeFilename(state.meeting?.topic)}-회의록.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function deleteMeeting() {
  if (!state.meetingId || dom.deleteConfirmInput.value.trim() !== "삭제") return;
  const id = state.meetingId;
  state.deletionInProgress = true;
  dom.confirmDeleteBtn.disabled = true;
  try {
    await api(meetingPath(), { method: "DELETE" });
    await removeMeetingAudio(id);
    localStorage.removeItem(audioSequenceKey(id));
    dom.deleteDialog.close();
    resetLocalState();
    showToast("회의와 관련 데이터를 영구 삭제했습니다.");
  } catch (error) {
    showToast(`삭제하지 못했습니다: ${error.message}`, "danger");
  } finally {
    state.deletionInProgress = false;
    dom.deleteConfirmInput.value = "";
    dom.confirmDeleteBtn.disabled = true;
  }
}

async function handleRemoteDeletion() {
  if (state.deletionInProgress) return;
  const id = state.meetingId;
  if (id) {
    await removeMeetingAudio(id);
    localStorage.removeItem(audioSequenceKey(id));
  }
  resetLocalState();
  showToast("회의가 다른 화면에서 영구 삭제되었습니다.");
}

function newMeeting() {
  state.audioUploadController?.abort();
  state.audioUploadController = null;
  closeMeetingSocket();
  closeRealtime();
  stopMediaTracks();
  rememberMeeting(null);
  state.meeting = null;
  state.minutes = null;
  state.transcript = [];
  state.finalizedIds.clear();
  state.transcriptArrivalSeq = 0;
  state.realtimeItemPrevious.clear();
  state.partials.clear();
  state.facts = [];
  state.decisions = [];
  state.actions = [];
  state.openQuestions = [];
  state.suggestions = [];
  state.progress = {};
  state.currentTopic = "";
  state.startedAt = null;
  state.endedAt = null;
  state.revision = 0;
  state.lastSeq = 0;
  state.seenEventIds.clear();
  state.pendingSocketMessages.clear();
  state.lastFactIds.clear();
  state.lastTranscriptIds.clear();
  state.unreadFacts = 0;
  state.unreadTranscript = 0;
  state.audioSequence = 0;
  state.audioPersistChain = Promise.resolve();
  state.audioSyncPending = false;
  state.minutesStale = false;
  state.speakerNames = {};
  dom.setupForm.reset();
  dom.micTestBtn.disabled = true;
  dom.meetingTitleHeader.textContent = "새 회의";
  renderMeetingData();
  hideBanner();
  setPhase("setup", { focus: true });
}

function resetLocalState() {
  stopMinutesPolling();
  newMeeting();
}

async function findRecoverableMeeting() {
  const result = await api("/api/meetings");
  const meetings = safeArray(result?.meetings ?? result);
  return meetings.find((meeting) => ["live", "paused", "finalizing"].includes(String(meeting?.lifecycle || "").toLowerCase())) || null;
}

async function restoreMeetingById(id) {
  rememberMeeting(id);
  state.socketStatus = "connecting";
  renderHealth();
  const snapshot = await api(meetingPath());
  applySnapshot(snapshot);
  const pendingAudio = await listAudioChunks(id).catch(() => []);
  setAudioSyncPending(pendingAudio.length > 0);
  await connectMeetingSocket();
  if (pendingAudio.length) flushAudioQueue();
  if (["live", "paused", "reconnecting"].includes(state.phase)) {
    state.phaseBeforeReconnect = state.phase === "paused" ? "paused" : "live";
    setPhase("reconnecting");
    showBanner("진행 중인 회의를 복원했습니다", "마이크는 개인정보 보호를 위해 자동으로 켜지지 않습니다.", {
      actionLabel: "마이크 다시 연결",
      action: reconnectAll,
      context: "microphone-restore",
    });
  } else if (state.phase === "finalizing") {
    startMinutesPolling();
  }
}

async function restoreMeeting() {
  const storedId = localStorage.getItem(ACTIVE_MEETING_KEY);
  let id = storedId;
  try {
    if (!id) {
      const recoverable = await findRecoverableMeeting();
      id = recoverable?.id || recoverable?.meeting_id || "";
    }
    if (!id) return;
    await restoreMeetingById(id);
  } catch (error) {
    if (storedId && error.status === 404) {
      rememberMeeting(null);
      try {
        const recoverable = await findRecoverableMeeting();
        const fallbackId = recoverable?.id || recoverable?.meeting_id || "";
        if (fallbackId && fallbackId !== storedId) {
          await restoreMeetingById(fallbackId);
          return;
        }
      } catch (fallbackError) {
        error = fallbackError;
      }
    }
    rememberMeeting(null);
    state.socketStatus = "error";
    setPhase("setup");
    showBanner("이전 회의를 불러오지 못했습니다", error.message, { tone: "danger" });
  }
}

function bindEvents() {
  dom.setupForm.addEventListener("submit", (event) => {
    event.preventDefault();
    createAndStartMeeting();
  });
  dom.startBtn.addEventListener("click", () => {
    if (["review", "completed"].includes(state.phase)) newMeeting();
    else if (state.phase === "error" && state.meetingId) reconnectAll();
    else dom.setupForm.requestSubmit();
  });
  dom.pauseBtn.addEventListener("click", togglePause);
  dom.stopBtn.addEventListener("click", () => dom.stopDialog.showModal());
  dom.confirmStopBtn.addEventListener("click", (event) => {
    event.preventDefault();
    dom.stopDialog.close();
    finalizeMeeting();
  });
  dom.micTestBtn.addEventListener("click", testMicrophone);
  dom.consentCheck.addEventListener("change", () => {
    dom.micTestBtn.disabled = !dom.consentCheck.checked;
  });
  dom.systemBannerAction.addEventListener("click", () => state.bannerAction?.());
  dom.transcriptBtn.addEventListener("click", () => openTranscript(dom.transcriptBtn));
  $$('[data-drawer]').forEach((button) => button.addEventListener("click", () => openDrawer(button.dataset.drawer, button)));
  $$('[data-close-drawer]').forEach((button) => button.addEventListener("click", closeDrawer));
  $$('[data-close-transcript]').forEach((button) => button.addEventListener("click", closeTranscript));
  dom.progressEvidenceBtn.addEventListener("click", () => {
    const [first] = dom.progressEvidenceBtn.dataset.evidenceIds.split(",").filter(Boolean);
    if (first) openTranscriptAt(first);
  });
  dom.newFactsBtn.addEventListener("click", () => {
    dom.factViewport.scrollTop = dom.factViewport.scrollHeight;
    state.unreadFacts = 0;
    dom.newFactsBtn.hidden = true;
  });
  dom.newTranscriptBtn.addEventListener("click", () => {
    dom.transcriptList.scrollTop = dom.transcriptList.scrollHeight;
    state.unreadTranscript = 0;
    dom.newTranscriptBtn.hidden = true;
  });
  dom.saveMinutesBtn.addEventListener("click", saveMinutes);
  dom.approveMinutesBtn.addEventListener("click", approveMinutes);
  dom.downloadMinutesBtn.addEventListener("click", downloadMinutes);
  dom.printMinutesBtn.addEventListener("click", () => {
    dom.minutesPrintContent.textContent = dom.minutesMarkdown.value;
    window.print();
  });
  dom.deleteMeetingBtn.addEventListener("click", () => {
    dom.deleteConfirmInput.value = "";
    dom.confirmDeleteBtn.disabled = true;
    dom.deleteDialog.showModal();
    requestAnimationFrame(() => dom.deleteConfirmInput.focus());
  });
  dom.deleteConfirmInput.addEventListener("input", () => {
    dom.confirmDeleteBtn.disabled = dom.deleteConfirmInput.value.trim() !== "삭제";
  });
  dom.confirmDeleteBtn.addEventListener("click", (event) => {
    event.preventDefault();
    deleteMeeting();
  });
  document.addEventListener("keydown", (event) => {
    trapDrawerFocus(event, dom.insightDrawer);
    trapDrawerFocus(event, dom.transcriptDrawer);
    if (event.key !== "Escape") return;
    if (dom.insightDrawer.getAttribute("aria-hidden") === "false") closeDrawer();
    else if (dom.transcriptDrawer.getAttribute("aria-hidden") === "false") closeTranscript();
  });
  window.addEventListener("online", reconnectAll);
  window.addEventListener("offline", () => {
    state.socketStatus = "offline";
    renderHealth();
    scheduleSocketReconnect();
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.mediaRecorder?.state === "recording") {
      try { state.mediaRecorder.requestData(); } catch {}
    }
    if (["live", "paused", "reconnecting", "finalizing"].includes(state.phase)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.mediaRecorder?.state === "recording") {
      try { state.mediaRecorder.requestData(); } catch {}
    }
  });
  navigator.mediaDevices?.addEventListener?.("devicechange", populateAudioDevices);
  bindDeveloperTools();
}

function bindDeveloperTools() {
  const params = new URLSearchParams(location.search);
  if (params.get("dev") === "1") dom.developerTools.hidden = false;
  const sendLegacy = (message) => {
    if (state.socket?.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify(message));
    else showToast("개발자 명령을 보낼 연결이 없습니다.", "danger");
  };
  $("#demoBtn")?.addEventListener("click", () => sendLegacy({ type: "play_demo_script", mode: $("#demoMode")?.value || "fixture" }));
  $("#pauseDemoBtn")?.addEventListener("click", () => sendLegacy({ type: "pause_demo_script" }));
  $("#resumeDemoBtn")?.addEventListener("click", () => sendLegacy({ type: "resume_demo_script" }));
  $("#stopDemoBtn")?.addEventListener("click", () => sendLegacy({ type: "stop_demo_script" }));
  $("#sampleBtn")?.addEventListener("click", () => sendLegacy({ type: "play_sample" }));
  $("#resetBtn")?.addEventListener("click", () => sendLegacy({ type: "reset" }));
  $("#micBtn")?.addEventListener("click", () => sendLegacy({ type: "start_mic" }));
}

async function checkServer() {
  try {
    await api("/health");
    state.socketStatus = state.meetingId ? state.socketStatus : "open";
  } catch {
    state.socketStatus = "error";
    showBanner("서버에 연결할 수 없습니다", "사내 good-listener 서버가 실행 중인지 확인해 주세요.", {
      tone: "danger",
      actionLabel: "서버 다시 확인",
      action: checkServer,
    });
  }
  renderHealth();
}

async function init() {
  bindEvents();
  state.timerHandle = window.setInterval(() => {
    updateTimer();
    if (state.progress?.updated_at) dom.progressUpdatedAt.textContent = formatRelativeTime(state.progress.updated_at);
  }, 1_000);
  const bootstrap = await api("/api/bootstrap");
  if (bootstrap?.ready === false || bootstrap?.openai_configured === false) {
    dom.setupStartBtn.disabled = true;
    showBanner("OpenAI 연결 설정이 필요합니다", "서버의 OPENAI_API_KEY를 설정한 뒤 다시 확인해 주세요.", {
      tone: "danger",
      assertive: true,
      actionLabel: "설정 다시 확인",
      action: () => location.reload(),
    });
  }
  await Promise.allSettled([checkServer(), populateAudioDevices()]);
  if (!window.isSecureContext && !["localhost", "127.0.0.1"].includes(location.hostname)) {
    showBanner("보안 연결이 필요합니다", "마이크는 HTTPS 또는 localhost에서만 사용할 수 있습니다.", {
      tone: "danger",
      assertive: true,
    });
    dom.setupStartBtn.disabled = true;
  }
  await restoreMeeting();
  renderMeetingData();
  setPhase(state.phase);
}

init().catch((error) => {
  setPhase("error");
  showBanner("앱을 준비하지 못했습니다", error.message, { tone: "danger", assertive: true });
});
