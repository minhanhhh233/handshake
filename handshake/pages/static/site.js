/* Handshake public pages: copy buttons and the playground's "Try it live" (document 09, 6a).
   Served from this origin only (CSP script-src 'self'). Writes text with textContent only. */
(function () {
  "use strict";

  // ---- copy buttons: <button data-copy="element-id">Copy</button>
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
    return Promise.resolve();
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    var target = document.getElementById(btn.getAttribute("data-copy"));
    if (!target) return;
    copyText(target.textContent.trim()).then(function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      btn.setAttribute("data-copied", "");
      setTimeout(function () { btn.textContent = old; btn.removeAttribute("data-copied"); }, 1500);
    });
  });

  // ---- playground: <article data-step="search"> ... <button class="try"> ... <pre class="res">
  var KEY = "handshake.playground";
  var session = null;
  try { session = JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { session = null; }

  function save() {
    try { sessionStorage.setItem(KEY, JSON.stringify(session)); } catch (e) { /* private mode */ }
  }
  function pretty(v) { return JSON.stringify(v, null, 2); }

  function run(card) {
    var step = card.getAttribute("data-step");
    var btn = card.querySelector(".try");
    var reqEl = card.querySelector(".req");
    var resEl = card.querySelector(".res");
    var stateEl = card.querySelector(".state");
    var youEl = card.querySelector(".you");
    btn.disabled = true;
    stateEl.textContent = "Running...";
    stateEl.setAttribute("data-state", "busy");
    fetch("/demo/playground/" + encodeURIComponent(step), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(session ? { trace_id: session.trace_id, k: session.k } : {}),
      credentials: "same-origin"
    }).then(function (r) {
      return r.json().then(function (body) { return { status: r.status, body: body }; });
    }).then(function (res) {
      var b = res.body || {};
      if (res.status === 200) {
        if (b.session) { session = b.session; save(); }
        if (b.request) reqEl.textContent = pretty(b.request);
        resEl.textContent = pretty(b.response);
        var failed = b.response && b.response.error;
        stateEl.textContent = failed ? "Gateway answered with an error" : "Done";
        stateEl.setAttribute("data-state", failed ? "bad" : "ok");
        if (youEl && b.you && b.you.approval_url) {
          var a = youEl.querySelector("a");
          a.href = b.you.approval_url;
          youEl.hidden = false;
        }
      } else {
        resEl.textContent = (b && b.message) ? b.message : ("HTTP " + res.status);
        stateEl.textContent = res.status === 429 ? "Limit reached" : "Not run";
        stateEl.setAttribute("data-state", "bad");
        if (res.status === 404 && b && b.code === "unknown_session") { session = null; save(); }
      }
    }).catch(function () {
      resEl.textContent = "The request did not reach the gateway. Check your connection.";
      stateEl.textContent = "Not run";
      stateEl.setAttribute("data-state", "bad");
    }).then(function () { btn.disabled = false; });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".try");
    if (!btn) return;
    var card = btn.closest("[data-step]");
    if (card) run(card);
  });

  var reset = document.getElementById("pg-reset");
  if (reset) {
    reset.addEventListener("click", function () {
      session = null;
      save();
      document.querySelectorAll("[data-step]").forEach(function (card) {
        card.querySelector(".res").textContent = "Not run yet.";
        var st = card.querySelector(".state");
        st.textContent = "";
        st.removeAttribute("data-state");
        var you = card.querySelector(".you");
        if (you) you.hidden = true;
      });
    });
  }
})();
