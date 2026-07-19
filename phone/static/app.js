/* laddy phone client. Plain JS, no build step, no external resources. */
"use strict";

var POLL_MS = 10000;

function $(id) { return document.getElementById(id); }

function token() { return localStorage.getItem("laddy_token") || ""; }

function flash(text, isError) {
  var el = $("flash");
  el.textContent = text;
  el.className = isError ? "error" : "muted";
  if (text) { setTimeout(function () { el.textContent = ""; }, 5000); }
}

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign(
    { "Authorization": "Bearer " + token() },
    opts.headers || {}
  );
  return fetch(path, opts).then(function (res) {
    if (res.status === 401) { throw new Error("unauthorized - check token"); }
    return res.json().then(function (body) {
      if (!res.ok) { throw new Error(body.error || ("HTTP " + res.status)); }
      return body;
    });
  });
}

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

/* -- questions ------------------------------------------------------------ */

function renderQuestions(questions) {
  var box = $("questions");
  var badge = $("questions-badge");
  badge.hidden = questions.length === 0;
  badge.textContent = String(questions.length);
  box.textContent = "";
  if (questions.length === 0) {
    var p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No pending questions.";
    box.appendChild(p);
    return;
  }
  questions.forEach(function (q) {
    var card = document.createElement("div");
    card.className = "card";
    var head = document.createElement("div");
    head.className = "card-task";
    head.textContent = q.task;
    var text = document.createElement("p");
    text.textContent = q.question;
    var answer = document.createElement("textarea");
    answer.rows = 3;
    answer.placeholder = "answer...";
    var send = document.createElement("button");
    send.textContent = "Send";
    send.addEventListener("click", function () {
      var value = answer.value.trim();
      if (!value) { flash("empty answer", true); return; }
      post("/api/answer", { task: q.task, id: q.id, answer: value })
        .then(function () { flash("answer sent: " + q.task, false); refreshQuestions(); })
        .catch(function (err) { flash(err.message, true); });
    });
    card.appendChild(head);
    card.appendChild(text);
    card.appendChild(answer);
    card.appendChild(send);
    box.appendChild(card);
  });
}

function refreshQuestions() {
  if (!token()) { return; }
  api("/api/questions")
    .then(function (body) { renderQuestions(body.questions || []); })
    .catch(function (err) { flash(err.message, true); });
}

/* -- status / queue / log ------------------------------------------------- */

function showText(id, promise) {
  promise
    .then(function (body) {
      var text = body.text || "";
      if (typeof body.rc === "number" && body.rc !== 0) {
        text = "[rc=" + body.rc + "]\n" + text;
      }
      $(id).textContent = text || "(empty)";
    })
    .catch(function (err) { flash(err.message, true); });
}

function wire() {
  var tokenInput = $("token");
  tokenInput.value = token();
  tokenInput.addEventListener("change", function () {
    localStorage.setItem("laddy_token", tokenInput.value.trim());
    flash("token saved", false);
    refreshQuestions();
  });

  $("status-refresh").addEventListener("click", function () {
    showText("status-out", api("/api/status"));
  });
  $("queue-refresh").addEventListener("click", function () {
    showText("queue-out", api("/api/queue"));
  });
  $("enqueue-btn").addEventListener("click", function () {
    var tasks = $("enqueue-tasks").value.trim().split(/\s+/).filter(Boolean);
    if (tasks.length === 0) { flash("no task ids", true); return; }
    showText("queue-out", post("/api/enqueue", {
      tasks: tasks,
      chain: $("enqueue-chain").checked
    }));
  });
  $("resume-btn").addEventListener("click", function () {
    var task = $("resume-task").value.trim();
    var reason = $("resume-reason").value.trim();
    if (!task || !reason) { flash("resume needs task + reason", true); return; }
    showText("queue-out", post("/api/resume", { task: task, reason: reason }));
  });
  $("log-btn").addEventListener("click", function () {
    var task = $("log-task").value.trim();
    if (!task) { flash("no task id", true); return; }
    showText("log-out", api("/api/log?task=" + encodeURIComponent(task)));
  });

  refreshQuestions();
  setInterval(refreshQuestions, POLL_MS);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }
}

document.addEventListener("DOMContentLoaded", wire);
