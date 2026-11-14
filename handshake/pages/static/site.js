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

/* Live agent demo: start a run, poll its steps, pair each call with the gateway's response.
   Everything is written with textContent / attributes; no HTML from the server is parsed. */
(function () {
  "use strict";
  var form = document.getElementById("lv-form");
  if (!form) return;
  var box = document.getElementById("lv-prompt");
  var go = document.getElementById("lv-go");
  var count = document.getElementById("lv-count");
  var list = document.getElementById("lv-steps");
  var human = document.getElementById("lv-human");
  var status = document.getElementById("lv-status");
  var available = form.getAttribute("data-available") === "yes";
  var run = null, after = 0, rows = {}, timer = null, startedAt = 0, lastOrder = "", ended = false;
  var cardBox = document.getElementById("lv-card");
  var timerBox = document.getElementById("lv-timer");
  var timerVal = document.getElementById("lv-timer-val");
  var tick = null, persona = "hotel";
  try { persona = sessionStorage.getItem("handshake.persona") || "hotel"; } catch (e) { persona = "hotel"; }

  // ---- timer: from "Run the agent" to the gateway's answer to place_order
  function timerStart() {
    clearInterval(tick);
    timerBox.hidden = false;
    timerBox.querySelector(".t-label").textContent = "Time to order";
    tick = setInterval(function () { timerVal.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + " s"; }, 100);
  }
  function timerStop(label) {
    if (tick === null) return;
    clearInterval(tick);
    tick = null;
    timerVal.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + " s";
    if (label) timerBox.querySelector(".t-label").textContent = label;
  }

  // ---- personas and the mandate card
  function renderCard(c, sandbox) {
    cardBox.textContent = "";
    var head = el("p", "who-is", c.label);
    if (c.customer) head.appendChild(el("span", "", " for " + c.customer));
    cardBox.appendChild(head);
    cardBox.appendChild(el("p", "", c.blurb));
    if (c.rules) {
      var dl = el("dl");
      c.rules.forEach(function (r) { dl.appendChild(el("dt", "", r.name)); dl.appendChild(el("dd", "", r.value)); });
      cardBox.appendChild(dl);
      var f = el("form");
      var lab = el("label", "", "Per-order cap (EUR)");
      lab.setAttribute("for", "cap-input");
      var inp = el("input");
      inp.id = "cap-input"; inp.type = "number"; inp.step = "0.01"; inp.min = "0.01"; inp.max = "100000";
      inp.value = c.cap || "";
      var save = el("button", "save", "Save cap");
      save.type = "submit";
      var msg = el("span", "msg", "");
      f.appendChild(lab); f.appendChild(inp); f.appendChild(save); f.appendChild(msg);
      f.addEventListener("submit", function (ev) {
        ev.preventDefault();
        save.disabled = true;
        fetch("/demo/sandbox/cap", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ persona: persona, cap: inp.value }), credentials: "same-origin" })
          .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
          .then(function (res) {
            save.disabled = false;
            if (res.status !== 200) { msg.className = "msg bad"; msg.textContent = res.body.message || "Not saved."; return; }
            renderCard(res.body.card, res.body.sandbox);
            var m = cardBox.querySelector(".msg");
            if (m) m.textContent = "Saved as mandate version " + res.body.card.mandate_version + ". Run the agent again.";
          })
          .catch(function () { save.disabled = false; msg.className = "msg bad"; msg.textContent = "Not saved."; });
      });
      cardBox.appendChild(f);
    }
    if (sandbox && sandbox.expires_at) {
      var t = new Date(sandbox.expires_at);
      cardBox.appendChild(el("p", "", "Your sandbox lasts until " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) + "."));
    }
  }
  function choose(slug) {
    persona = slug;
    try { sessionStorage.setItem("handshake.persona", slug); } catch (e) { /* private mode */ }
    document.querySelectorAll(".pb[data-persona]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-persona") === slug ? "true" : "false");
    });
    fetch("/demo/sandbox?persona=" + encodeURIComponent(slug), { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 200) { cardBox.textContent = ""; cardBox.appendChild(el("p", "", res.body.message || "The sandbox could not be loaded.")); return; }
        renderCard(res.body.card, res.body.sandbox);
      })
      .catch(function () { cardBox.textContent = ""; cardBox.appendChild(el("p", "", "The sandbox could not be loaded.")); });
  }
  document.querySelectorAll(".pb[data-persona]").forEach(function (b) {
    b.addEventListener("click", function () { if (!go.disabled || !available) choose(b.getAttribute("data-persona")); });
  });
  if (!document.querySelector('.pb[data-persona="' + persona + '"]')) persona = "hotel";
  choose(persona);

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function setStatus(text, bad) {
    status.textContent = text || "";
    if (bad) status.setAttribute("data-state", "bad"); else status.removeAttribute("data-state");
  }
  function lock(on) {
    box.disabled = on || !available;
    go.disabled = on || !available;
    form.querySelectorAll(".chip").forEach(function (c) { c.disabled = on || !available; });
  }
  function updateCount() { count.textContent = box.value.length + " / " + box.maxLength; }
  box.addEventListener("input", updateCount);
  form.addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip");
    if (!chip || chip.disabled) return;
    box.value = chip.getAttribute("data-example");
    updateCount();
    box.focus();
  });

  function jsonBlock(value) {
    var wrap = document.createDocumentFragment();
    var pre = el("pre", "", JSON.stringify(value, null, 2));
    wrap.appendChild(pre);
    var lines = pre.textContent.split("\n").length;
    if (lines > 14) {
      pre.className = "clamp";
      var label = "Expand (" + lines + " lines)";
      var more = el("button", "more", label);
      more.type = "button";
      more.addEventListener("click", function () {
        var open = pre.className === "clamp";
        pre.className = open ? "" : "clamp";
        more.textContent = open ? "Collapse" : label;
      });
      wrap.appendChild(more);
    }
    return wrap;
  }
  function row(kind) {
    var li = el("li", "lv-row " + kind);
    var req = el("div", "lv-cell req");
    var res = el("div", "lv-cell res empty");
    li.appendChild(req);
    li.appendChild(res);
    list.appendChild(li);
    return { li: li, req: req, res: res };
  }
  function who(left, right) {
    var w = el("div", "who");
    w.appendChild(el("span", "", left));
    if (right) w.appendChild(el("span", "", right));
    return w;
  }
  function fillRes(r, title, value, bad) {
    r.res.className = "lv-cell res" + (bad ? " bad" : "");
    r.res.textContent = "";
    r.res.appendChild(who("Handshake", title));
    r.res.appendChild(jsonBlock(value));
  }
  function addStep(s) {
    var r;
    if (s.kind === "user") {
      r = row("user");
      r.req.appendChild(who("Visitor request"));
      r.req.appendChild(el("p", "say", s.summary));
    } else if (s.kind === "text") {
      r = row("text");
      r.req.appendChild(who("External buyer agent"));
      r.req.appendChild(el("p", "say", s.summary));
    } else if (s.kind === "call") {
      r = row("call");
      r.req.appendChild(who("External buyer agent", "call " + s.n));
      r.req.appendChild(el("p", "tool", s.title));
      r.req.appendChild(jsonBlock((s.request || {}).input || {}));
      r.res.className = "lv-cell res";
      r.res.appendChild(el("p", "wait", "Waiting for the gateway..."));
      rows[s.n] = r;
    } else if (s.kind === "result") {
      if (s.title === "place_order") timerStop("Time to order");
      r = rows[s.n];
      if (!r) { r = row("call"); r.req.appendChild(el("p", "tool", s.title)); }
      fillRes(r, s.status === "bad" ? "error" : "response", s.detail, s.status === "bad");
    } else if (s.kind === "verify") {
      r = row("verify");
      r.req.appendChild(who("Signature check"));
      r.req.appendChild(el("p", "tool", "verify_offer (EdDSA, published key)"));
      r.req.appendChild(jsonBlock(s.request || {}));
      fillRes(r, s.status === "bad" ? "not verified" : "verified", s.detail, s.status === "bad");
    } else if (s.kind === "end") {
      timerStop("Run time, no order");
      ended = true;
      setStatus(s.summary, s.status === "bad");
    }
  }

  function showOrder(o, runUrl) {
    var key = o ? o.status + (o.approval_url || "") : "";
    if (key === lastOrder) return;
    lastOrder = key;
    human.textContent = "";
    if (!o) return;
    var card = el("section", "lv-card panel");
    if (o.status === "pending_approval" && o.approval_url) {
      if (o.qr) card.className += " has-qr";
      card.appendChild(el("h3", "", "Your turn: approve this order"));
      card.appendChild(el("p", "", "The total is over the agent's limit, so a person must approve it. In this demo that person is you: open the approval page on this device, or scan the code with your phone."));
      var p = el("p");
      var a = el("a", "btn", "Open the approval page");
      a.href = o.approval_url;
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      p.appendChild(a);
      card.appendChild(p);
      if (o.qr) {
        var q = el("div", "qr");
        var img = el("img");
        img.src = o.qr;
        img.alt = "QR code for the approval page";
        q.appendChild(img);
        card.appendChild(q);
      }
      card.appendChild(el("p", "muted", "Waiting for your decision. This page checks every few seconds."));
    } else {
      var titles = {
        confirmed: "The order is confirmed.",
        declined: "Declined. Nothing was ordered.",
        expired: "The approval request expired. Nothing was ordered.",
        rejected: "The gateway rejected this order."
      };
      if (o.status !== "confirmed") card.className += " stopped";
      if (ended) setStatus("");
      card.appendChild(el("h3", "", titles[o.status] || ("Order status: " + o.status)));
      if (o.receipt_url) {
        var p2 = el("p");
        var r2 = el("a", "btn", "Open the signed receipt");
        r2.href = o.receipt_url;
        r2.rel = "noreferrer";
        p2.appendChild(r2);
        card.appendChild(p2);
      }
    }
    if (runUrl) {
      var p3 = el("p", "muted");
      var l = el("a", "", "Open this run as a page");
      l.href = runUrl;
      p3.appendChild(l);
      card.appendChild(p3);
    }
    human.appendChild(card);
  }

  function poll() {
    if (!run) return;
    fetch("/demo/live/" + encodeURIComponent(run.trace_id) + "?k=" + encodeURIComponent(run.k) +
          "&after=" + after, { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 200) {
          setStatus((res.body && res.body.message) || "The run could not be read.", true);
          lock(false);
          return;
        }
        var b = res.body;
        (b.steps || []).forEach(addStep);
        after = b.next;
        showOrder(b.order, b.run_url);
        var pending = b.order && b.order.status === "pending_approval";
        if (b.state === "running") {
          setStatus("The agent is working... " + Math.round((Date.now() - startedAt) / 1000) + " s");
          timer = setTimeout(poll, 1500);
          return;
        }
        if (b.state === "stopped" && !ended) {
          setStatus("The run stopped unexpectedly. Try again, or use a scripted demo below.", true);
        }
        lock(false);
        if (pending && Date.now() - startedAt < 35 * 60 * 1000) timer = setTimeout(poll, 4000);
      })
      .catch(function () { timer = setTimeout(poll, 4000); });
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = box.value.trim();
    if (!text) { box.focus(); return; }
    clearTimeout(timer);
    list.textContent = "";
    human.textContent = "";
    rows = {};
    after = 0;
    lastOrder = "";
    ended = false;
    lock(true);
    setStatus("Starting the agent...");
    fetch("/demo/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text, persona: persona }),
      credentials: "same-origin"
    }).then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 202) {
          setStatus((res.body && res.body.message) || ("HTTP " + res.status), true);
          lock(false);
          return;
        }
        run = res.body;
        startedAt = Date.now();
        timerStart();
        setStatus("The agent is working...");
        poll();
      })
      .catch(function () { setStatus("The request did not reach the gateway.", true); lock(false); });
  });
  updateCount();
})();
