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
  var tick = null, persona = "hotel", sandboxQueue = Promise.resolve();
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
    if (c.rules) cardBox.appendChild(el("p", "set-by", "Set by the customer's purchasing manager (you, in this demo)"));
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
      if (c.preferences) {
        var pr = el("div", "prefs");
        pr.appendChild(el("span", "chips-h", "Rank offers by"));
        c.preferences.forEach(function (p) {
          var pb = el("button", "pref", p.label);
          pb.type = "button";
          pb.setAttribute("aria-pressed", c.preference === p.id ? "true" : "false");
          pb.addEventListener("click", function () {
            pb.disabled = true;
            fetch("/demo/sandbox/preference", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ persona: persona, preference: p.id }), credentials: "same-origin" })
              .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
              .then(function (res) {
                if (res.status === 200) {
                  renderCard(res.body.card, res.body.sandbox);
                  var m2 = cardBox.querySelector(".msg");
                  if (m2) m2.textContent = "Ranking set (mandate version " + res.body.card.mandate_version + "). Try \"Compare three offers\".";
                } else { pb.disabled = false; }
              })
              .catch(function () { pb.disabled = false; });
          });
          pr.appendChild(pb);
        });
        cardBox.appendChild(pr);
      }
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
    // One sandbox request at a time, so the first response's cookie is set before the next
    // request: otherwise two sandboxes could be created and a running run lost.
    var req = sandboxQueue.then(function () {
      return fetch("/demo/sandbox?persona=" + encodeURIComponent(slug), { credentials: "same-origin" });
    });
    sandboxQueue = req.catch(function () { /* keep the queue going */ });
    return req
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 200) { cardBox.textContent = ""; cardBox.appendChild(el("p", "", res.body.message || "The sandbox could not be loaded.")); return; }
        var approver = slug === "approver";
        form.hidden = approver;
        inboxBox.hidden = !approver;
        if (approver) {
          cardBox.textContent = "";
          cardBox.appendChild(el("p", "who-is", "Approver"));
          cardBox.appendChild(el("p", "", "You are the customers' purchasing manager. Orders over a mandate's limits wait here for your decision."));
          renderInbox(res.body.inbox || []);
          return;
        }
        renderCard(res.body.card, res.body.sandbox);
        renderScenarios(res.body.scenarios);
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
    var bad = badBox && badBox.checked;
    box.disabled = on || !available || bad;
    go.disabled = on || (!available && !bad);
    form.querySelectorAll(".chip").forEach(function (c) {
      c.disabled = on || (!available && !c.hasAttribute("data-scenario"));
    });
    if (badBox) badBox.disabled = on;
  }
  function updateCount() { count.textContent = box.value.length + " / " + box.maxLength; }
  box.addEventListener("input", updateCount);
  form.addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip");
    if (!chip || chip.disabled) return;
    if (chip.hasAttribute("data-scenario")) {
      startRun("/demo/scenario", { id: chip.getAttribute("data-scenario"), persona: persona },
               "Running the scripted scenario...");
      return;
    }
    box.value = chip.getAttribute("data-example");
    updateCount();
    box.focus();
  });
  var badBox = document.getElementById("lv-bad");
  var badWrap = document.getElementById("lv-bad-wrap");
  var inboxBox = document.getElementById("lv-inbox");
  if (badBox) badBox.addEventListener("change", function () {
    go.textContent = badBox.checked ? "Run the misbehaving agent" : "Run the agent";
    lock(false);
  });
  function renderScenarios(sc) {
    var scripted = document.getElementById("lv-scripted");
    var prompts = document.getElementById("lv-prompts");
    scripted.textContent = "";
    prompts.textContent = "";
    ((sc && sc.scripted) || []).forEach(function (x) {
      var b = el("button", "chip scripted", x.label);
      b.type = "button";
      b.setAttribute("data-scenario", x.id);
      scripted.appendChild(b);
    });
    ((sc && sc.prompts) || []).forEach(function (t) {
      var b = el("button", "chip", t);
      b.type = "button";
      b.setAttribute("data-example", t);
      prompts.appendChild(b);
    });
    if (badWrap) badWrap.hidden = !(sc && sc.misbehaving);
    if (badBox && badWrap && badWrap.hidden && badBox.checked) {
      badBox.checked = false;
      go.textContent = "Run the agent";
    }
    lock(false);
  }
  function renderInbox(items) {
    inboxBox.textContent = "";
    inboxBox.appendChild(el("p", "chips-h", "Approval requests in your sandbox"));
    if (!items.length) {
      inboxBox.appendChild(el("p", "", "Nothing is waiting. Run a scenario that goes over a limit, such as the Hotel chain's example milk order, then come back."));
      return;
    }
    items.forEach(function (it) {
      var d = el("div", "item");
      d.appendChild(el("h4", "", it.customer + ": " + it.total_excl_tax + " EUR excl. tax"));
      if (it.persona) d.appendChild(el("p", "", it.persona + (it.reference ? ", reference " + it.reference : "") + ", delivery " + it.delivery_date));
      (it.reasons || []).forEach(function (r) { d.appendChild(el("p", "", r)); });
      d.appendChild(el("p", "", "Expires " + new Date(it.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) + "."));
      var acts = el("div", "acts");
      [["approve", "Approve", ""], ["decline", "Decline", "no"]].forEach(function (a) {
        var b = el("button", a[2], a[1]);
        b.type = "button";
        b.addEventListener("click", function () {
          acts.querySelectorAll("button").forEach(function (x) { x.disabled = true; });
          fetch(it.approve_path, { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ decision: a[0] }).toString() })
            .then(function () { choose("approver"); })
            .catch(function () { choose("approver"); });
        });
        acts.appendChild(b);
      });
      d.appendChild(acts);
      inboxBox.appendChild(d);
    });
  }

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
      r.req.appendChild(who(s.title === "Scenario" ? "Scenario" : "Visitor request"));
      r.req.appendChild(el("p", "say", s.summary));
    } else if (s.kind === "text") {
      r = row("text");
      r.req.appendChild(who(s.title === "Scripted agent" ? "External buyer agent (scripted, no AI)" : "External buyer agent"));
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
      var opts = s.title === "request_offer" && s.detail && s.detail.options;
      if (opts && opts.length > 1) compareRow(opts);
    } else if (s.kind === "verify") {
      r = row("verify");
      r.req.appendChild(who("Signature check"));
      r.req.appendChild(el("p", "tool", "verify_offer (EdDSA, published key)"));
      r.req.appendChild(jsonBlock(s.request || {}));
      if (s.request && s.request.jws) r.req.appendChild(verifyTool(s.request.jws));
      fillRes(r, s.status === "bad" ? "not verified" : "verified", s.detail, s.status === "bad");
    } else if (s.kind === "end") {
      timerStop("Run time, no order");
      ended = true;
      setStatus(s.summary, s.status === "bad");
    }
  }

  // ---- comparison of ranked options (stage 3): side by side, rows without data omitted
  function weighted(o, get) {
    var tot = 0, w = 0;
    (o.offer.lines || []).forEach(function (ln) {
      var v = get(ln);
      if (v === null || v === undefined) return;
      var q = parseFloat(ln.line_total_excl_tax) || 0;
      tot += v * q; w += q;
    });
    return w ? tot / w : null;
  }
  function compareRow(opts) {
    var li = el("li", "lv-row compare");
    var cell = el("div", "lv-cell cmp-cell");
    cell.appendChild(el("div", "who", "Handshake: ranked options side by side"));
    var wrap = el("div", "cmp-wrap");
    var t = el("table", "cmp");
    var head = el("tr");
    head.appendChild(el("th", "", ""));
    opts.forEach(function (o) { head.appendChild(el("th", "opt-" + o.label, o.label)); });
    t.appendChild(head);
    function line(label, values) {
      if (values.every(function (v) { return v === null || v === "" || v === undefined; })) return;
      var tr = el("tr");
      tr.appendChild(el("th", "", label));
      values.forEach(function (v) { tr.appendChild(el("td", "", v === null || v === undefined ? "" : String(v))); });
      t.appendChild(tr);
    }
    line("Total excl. tax", opts.map(function (o) { return o.offer.totals.excl_tax + " EUR"; }));
    line("Delivery date", opts.map(function (o) { return o.offer.delivery_date; }));
    line("Products", opts.map(function (o) {
      return (o.offer.lines || []).map(function (ln) { return ln.quantity.value + " x " + ln.product_id; }).join(", ");
    }));
    function partial(o, key) {
      return (o.offer.lines || []).some(function (ln) { var c = ln.comparison || {}; return c[key] === null || c[key] === undefined; });
    }
    line("Delivery reliability", opts.map(function (o) {
      var v = weighted(o, function (ln) { return (ln.comparison || {}).delivery_reliability; });
      return v === null ? null : Math.round(v * 100) + "% of ordered quantity delivered" +
        (partial(o, "delivery_reliability") ? " (known for some products only)" : "");
    }));
    line("Labels", opts.map(function (o) {
      var set = {};
      (o.offer.lines || []).forEach(function (ln) { ((ln.attributes || {}).labels || []).forEach(function (l) { set[l] = 1; }); });
      return Object.keys(set).map(function (l) { return l.toLowerCase().replace(/_/g, " "); }).join(", ");
    }));
    line("Carbon (group average)", opts.map(function (o) {
      var v = weighted(o, function (ln) { return (ln.comparison || {}).carbon_kg_co2e_per_kg; });
      return v === null ? null : v.toFixed(2) + " kg CO2e per kg";
    }));
    line("Score", opts.map(function (o) {
      var sc = o.score;
      if (!sc) return null;
      var parts = Object.keys(sc.breakdown || {}).filter(function (k) { return sc.breakdown[k] > 0; })
        .map(function (k) { return k.replace(/_/g, " ") + " " + sc.breakdown[k].toFixed(2); });
      return sc.total.toFixed(2) + (parts.length ? " (" + parts.join(", ") + ")" : "");
    }));
    wrap.appendChild(t);
    cell.appendChild(wrap);
    cell.appendChild(el("p", "cmp-note", "Best follows the customer's ranking preference; better is the cheapest; good uses the fewest products. Options that would be identical are left out. Ranking never changes what is allowed."));
    li.appendChild(cell);
    list.appendChild(li);
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
          setStatus("Working... " + Math.round((Date.now() - startedAt) / 1000) + " s");
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

  function startRun(url, payload, label) {
    clearTimeout(timer);
    list.textContent = "";
    human.textContent = "";
    rows = {};
    after = 0;
    lastOrder = "";
    ended = false;
    lock(true);
    setStatus("Starting...");
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 202) {
          setStatus((res.body && res.body.message) || ("HTTP " + res.status), true);
          lock(false);
          return;
        }
        run = res.body;
        startedAt = Date.now();
        timerStart();
        setStatus(label);
        poll();
      })
      .catch(function () { setStatus("The request did not reach the gateway.", true); lock(false); });
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (badBox && badBox.checked) {
      startRun("/demo/scenario", { id: "misbehaving", persona: persona }, "The misbehaving agent is trying...");
      return;
    }
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
  // ---- "Start here" strip: four first steps, one click each
  var startNote = document.getElementById("start-note");
  function live() { document.getElementById("live").scrollIntoView({ behavior: "smooth", block: "start" }); }
  document.querySelectorAll("[data-start]").forEach(function (b) {
    b.addEventListener("click", function () {
      var what = b.getAttribute("data-start");
      if (startNote) startNote.textContent = "";
      if (run && !ended && go.disabled) { if (startNote) startNote.textContent = "Wait for the current run to finish."; return; }
      b.classList.add("done");
      if (what === "example") {
        choose("hotel").then(function () {
          if (badBox) { badBox.checked = false; go.textContent = "Run the agent"; }
          live();
          startRun("/demo/scenario", { id: "hotel_milk", persona: "hotel" }, "Running the scripted scenario...");
        });
      } else if (what === "misbehave") {
        var p = (persona === "visitor" || persona === "approver") ? "restaurant" : persona;
        choose(p).then(function () {
          if (badBox) { badBox.checked = true; go.textContent = "Run the misbehaving agent"; }
          live();
          startRun("/demo/scenario", { id: "misbehaving", persona: p }, "The misbehaving agent is trying...");
        });
      } else if (what === "school") {
        choose("school").then(function () {
          if (badBox) { badBox.checked = false; go.textContent = "Run the agent"; }
          live();
          startRun("/demo/scenario", { id: "school_milk", persona: "school" }, "Running the scripted scenario...");
        });
      } else if (what === "verify") {
        var vb = document.querySelector(".vtool button");
        if (!vb) {
          b.classList.remove("done");
          if (startNote) startNote.textContent = "Run step 1 first: its offer carries the signature to check.";
          return;
        }
        vb.closest(".lv-row").scrollIntoView({ behavior: "smooth", block: "center" });
        vb.click();
      }
    });
  });

  // ---- offer verification the visitor can do: Ed25519 in this browser, against the JWKS
  var jwksCache = null;
  function b64u(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function toB64u(bytes) {
    var bin = "";
    bytes.forEach(function (b) { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function verifyJws(jws) {
    var parts = jws.split(".");
    if (parts.length !== 3 || !window.crypto || !crypto.subtle) return Promise.reject(new Error("unsupported"));
    var header = JSON.parse(new TextDecoder().decode(b64u(parts[0])));
    var keys = jwksCache ? Promise.resolve(jwksCache) :
      fetch("/.well-known/jwks.json").then(function (r) { return r.json(); }).then(function (j) { jwksCache = j; return j; });
    return keys.then(function (set) {
      var jwk = (set.keys || []).filter(function (k) { return k.kid === header.kid; })[0];
      if (!jwk || header.alg !== "EdDSA") return false;
      return crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"])
        .then(function (key) {
          return crypto.subtle.verify({ name: "Ed25519" }, key, b64u(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
        });
    });
  }
  function editPrice(jws) {
    var parts = jws.split(".");
    var payload = JSON.parse(new TextDecoder().decode(b64u(parts[1])));
    var line = (payload.lines || [])[0];
    if (line) line.unit_price_excl_tax = (Math.max(0.01, parseFloat(line.unit_price_excl_tax) - 0.1)).toFixed(2);
    return { jws: parts[0] + "." + toB64u(new TextEncoder().encode(JSON.stringify(payload))) + "." + parts[2],
             price: line ? line.unit_price_excl_tax : "?" };
  }
  function verifyTool(jws) {
    var wrap = el("div", "vtool");
    var a = el("button", "", "Verify in this browser");
    var b = el("button", "", "Edit the price, verify again");
    a.type = b.type = "button";
    var out = el("span", "vres", "");
    function show(p, label) {
      out.className = "vres";
      out.textContent = "Checking...";
      p.then(function (ok) {
        out.className = "vres " + (ok ? "ok" : "bad");
        out.textContent = label + (ok ? ": signature valid" : ": signature INVALID");
      }).catch(function () {
        out.className = "vres bad";
        out.textContent = "This browser cannot check Ed25519 signatures.";
      });
    }
    a.addEventListener("click", function () { show(verifyJws(jws), "Signed offer"); });
    b.addEventListener("click", function () {
      var t = editPrice(jws);
      show(verifyJws(t.jws), "Price changed to " + t.price);
    });
    wrap.appendChild(a); wrap.appendChild(b); wrap.appendChild(out);
    return wrap;
  }
  updateCount();
})();
