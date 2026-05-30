const connection = document.querySelector("#connection");
const statusLine = document.querySelector("#statusLine");
const transcript = document.querySelector("#transcript");
const prepForm = document.querySelector("#prepForm");
const prepTopic = document.querySelector("#prepTopic");
const prepGoal = document.querySelector("#prepGoal");
const prepTerms = document.querySelector("#prepTerms");
const contextPill = document.querySelector("#contextPill");
const form = document.querySelector("#utteranceForm");
const input = document.querySelector("#utteranceInput");

let socket;
let micRunning = false;

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
    if (payload.type === "status") statusLine.textContent = payload.message;
  });
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify(message));
}

function renderState(state) {
  statusLine.textContent = state.running ? "회의 진행 중" : "대기 중";
  micRunning = state.mic_running;
  document.querySelector("#micBtn").textContent = micRunning ? "마이크 중지" : "마이크";

  Object.values(state.panels).forEach(updatePanel);

  const enabled = new Set(state.enabled_panels);
  document.querySelectorAll("[data-panel-toggle]").forEach((toggle) => {
    toggle.checked = enabled.has(toggle.dataset.panelToggle);
  });

  renderContext(state.context || {});

  transcript.innerHTML = "";
  state.transcript.forEach(addTranscript);
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

  node.dataset.importance = String(panel.importance || 1);
  node.dataset.status = panel.status || "idle";
  node.querySelector(".panel-text").textContent = panel.text || "";
  node.querySelector('[data-role="reason"]').textContent = panel.reason || "";

  const provider = node.querySelector('[data-role="provider"]');
  const elapsed = panel.elapsed_s ? `${Number(panel.elapsed_s).toFixed(1)}s` : "";
  provider.textContent = [panel.provider, elapsed].filter(Boolean).join(" · ");
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

document.querySelector("#startBtn").addEventListener("click", () => send({ type: "start" }));
document.querySelector("#stopBtn").addEventListener("click", () => send({ type: "stop" }));
document.querySelector("#sampleBtn").addEventListener("click", () => send({ type: "play_sample" }));
document.querySelector("#resetBtn").addEventListener("click", () => send({ type: "reset" }));
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
  send({ type: "utterance", text });
  input.value = "";
  input.focus();
});

connect();
