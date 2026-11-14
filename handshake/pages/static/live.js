/* Handshake live demo page (/live): personas, rules, two frames with playback.
   Same-origin only (CSP script-src 'self'). Text is written with textContent only. */
(function () {
  "use strict";
  var form = document.getElementById("lv-form");
  if (!form) return;
  var box = document.getElementById("lv-prompt");
  var go = document.getElementById("lv-go");
  var count = document.getElementById("lv-count");
  var human = document.getElementById("lv-human");
  var statusEl = document.getElementById("lv-status");
  var frameBuyer = document.getElementById("frame-buyer");
  var frameGw = document.getElementById("frame-gw");
  var rolePanel = document.getElementById("role-panel");
  var rulesWho = document.getElementById("rules-who");
  var rulesList = document.getElementById("rules-list");
  var editBox = document.getElementById("edit-box");
  var editBtn = document.getElementById("rules-edit");
  var groupsBox = document.getElementById("scen-groups");
  var logEl = document.getElementById("log");
  var available = form.getAttribute("data-available") === "yes";
  var pb = {
    count: document.getElementById("pb-count"), prev: document.getElementById("pb-prev"),
    next: document.getElementById("pb-next"), play: document.getElementById("pb-play"),
    speed: document.getElementById("pb-speed"), jump: document.getElementById("pb-jump"),
    all: document.getElementById("pb-all")
  };
  var timerBox = document.getElementById("lv-timer");
  var timerVal = document.getElementById("lv-timer-val");

  var persona = "hotel", card = null, scen = null, sandboxQueue = Promise.resolve();
  try { persona = sessionStorage.getItem("handshake.persona") || "hotel"; } catch (e) { persona = "hotel"; }
  var run = null, after = 0, steps = [], cursor = -1, following = true, playing = null;
  var running = false, ended = false, pollTimer = null, startedAt = 0, tick = null, lastOrder = "";
  var verifyWhenReady = false;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function setStatus(text, bad) {
    statusEl.textContent = text || "";
    if (bad) statusEl.setAttribute("data-state", "bad"); else statusEl.removeAttribute("data-state");
  }
  function json(v) { return el("pre", "", JSON.stringify(v, null, 2)); }
  function money(v) { var n = parseFloat(v); return isNaN(n) ? String(v) : n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function hm(iso) { try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); } catch (e) { return iso; } }
  function post(url, payload) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); });
  }
  function lock(on) {
    running = on;
    box.disabled = on || !available;
    go.disabled = on || !available;
    form.querySelectorAll('input[name="behaviour"]').forEach(function (x) { x.disabled = on || !available; });
    groupsBox.querySelectorAll(".scen").forEach(function (b) {
      b.disabled = on || (!available && b.getAttribute("data-live") === "1");
    });
  }
  function updateCount() { count.textContent = box.value.length + " / " + box.maxLength; }
  box.addEventListener("input", updateCount);

  // ---------------------------------------------------------------- timer
  function timerStart() {
    clearInterval(tick);
    timerBox.hidden = false;
    timerBox.querySelector(".t-label").textContent = "Time to order";
    tick = setInterval(function () { timerVal.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + " s"; }, 100);
  }
  function timerStop(label) {
    if (tick === null) return;
    clearInterval(tick); tick = null;
    timerVal.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + " s";
    if (label) timerBox.querySelector(".t-label").textContent = label;
  }

  // ---------------------------------------------------------------- summaries (one sentence per step)
  var BUYER = { user: 1, text: 1, call: 1, alter: 1, inject: 1, probe: 1 };
  var GATEWAY = { result: 1, verify: 1, seller: 1 };
  function firstSentence(t) {
    var s = String(t || "").replace(/\s+/g, " ").trim();
    var m = s.match(/^(.{20,220}?[.!?])(\s|$)/);
    return m ? m[1] : (s.length > 200 ? s.slice(0, 197) + "..." : s);
  }
  function callSummary(name, input) {
    input = input || {};
    var lines = input.lines || [];
    if (name === "search_products") {
      var parts = [];
      if (input.q) parts.push('"' + input.q + '"');
      if (input.category) parts.push("category " + input.category);
      if (input.claims && input.claims.length) parts.push(input.claims.join(", ").toLowerCase().replace(/_/g, " "));
      return "Searches the catalogue" + (parts.length ? " for " + parts.join(", ") : "") + ".";
    }
    if (name === "request_offer") {
      var q = lines.map(function (l) { return l.quantity ? l.quantity.value + " " + l.quantity.unit : "?"; }).join(", ");
      return "Asks for a signed offer: " + lines.length + " line" + (lines.length === 1 ? "" : "s") + " (" + q + ")" +
        (input.delivery_date ? ", delivery " + input.delivery_date : "") + (input.max_options > 1 ? ", up to " + input.max_options + " options" : "") + ".";
    }
    if (name === "dry_run_order") return "Checks the offer against its mandate without ordering (dry run).";
    if (name === "place_order") return "Places the order" + (input.reference ? " (reference " + input.reference + ")" : "") + ".";
    if (name === "get_mandate") return "Reads its mandate.";
    if (name === "get_order") return "Checks the order's status.";
    if (name === "get_receipt") return "Fetches the signed receipt.";
    if (name === "get_product") return "Reads product " + (input.product_id || "") + ".";
    if (name === "list_proposals") return "Lists proactive offers sent to it.";
    if (name === "decline_proposal") return "Declines a proactive offer.";
    if (name === "raw_request" || /POST|GET|PUT/.test(name)) return "Sends a raw request: " + (input.method || "") + " " + (input.path || name) + ".";
    if (name === "verify_offer") return "Checks an offer's signature.";
    return "Calls " + name + ".";
  }
  function resultSummary(s) {
    var d = s.detail || {};
    var name = s.title;
    if (s.status === "bad") {
      var code = d.code || d.error || (d.body && d.body.code) || ("HTTP " + (d.status || ""));
      var why = d.detail || d.message || (d.body && (d.body.detail || d.body.message)) || "";
      return "Refused: " + code + (why ? ". " + firstSentence(why) : ".");
    }
    if (name === "request_offer" && d.options) {
      if (d.options.length > 1) {
        return d.options.length + " ranked signed offers: " + d.options.map(function (o) { return o.label + " " + money(o.offer.totals.excl_tax) + " EUR"; }).join(", ") + ".";
      }
      var o = d.options[0].offer, f = (o.fulfilment || [])[0] || {};
      var mins = Math.round((new Date(o.expires_at) - new Date(o.issued_at)) / 60000);
      var partial = (o.fulfilment || []).some(function (x) { return x.complete === false; });
      return "Signed offer: " + money(o.totals.excl_tax) + " EUR excl. tax, valid " + mins + " minutes" +
        (partial ? "; only " + f.offered.value + " of " + f.requested.value + " " + f.requested.unit + " can be delivered" : "") + ".";
    }
    if (name === "dry_run_order") {
      var r = (d.reasons || []).map(function (x) { return x.code.replace(/_/g, " "); });
      return "Dry run: " + String(d.outcome).replace(/_/g, " ") + (r.length ? " (" + r.join(", ") + ")" : "") + ".";
    }
    if (name === "place_order") return "Order " + String(d.status).replace(/_/g, " ") + (d.totals ? ": " + money(d.totals.excl_tax) + " EUR excl. tax" : "") + ".";
    if (name === "search_products") return (d.count !== undefined ? d.count : (d.items || []).length) + " products found; search never shows prices or stock.";
    if (name === "get_mandate") return "Mandate version " + d.version + ": " + (d.limits && d.limits.max_order_total_excl_tax ? "per order " + money(d.limits.max_order_total_excl_tax) + " EUR" : "no per-order cap") + ".";
    if (name === "list_proposals") return d.count + " open proactive offer" + (d.count === 1 ? "" : "s") + ".";
    if (name === "raw_request") return "HTTP " + d.status + ".";
    if (name === "get_order") return "Order status: " + String(d.status).replace(/_/g, " ") + ".";
    return "Answer to " + name + ".";
  }
  function summarize(s) {
    var d = s.detail || {};
    switch (s.kind) {
      case "user": return s.title === "Scenario" ? "Scenario: " + s.summary + "." : "You asked your agent: " + s.summary;
      case "text": return firstSentence(s.summary);
      case "call": return callSummary(s.title, (s.request || {}).input);
      case "result": return resultSummary(s);
      case "verify": return d.verified ? "Signature verified with the published key " + ((d.header || {}).kid || "") + "." : "Signature check failed.";
      case "seller": return d.status === "open" ? "Valio Aimo's selling agent sent a proactive offer." : "Handshake blocked a proactive offer: " + (d.reason || "") ;
      case "probe": return "Probe " + s.title + ": " + ((d.verdict) || "") + ".";
      case "alter": return "A faulty client changed the request before it reached Handshake.";
      case "inject": return "A hidden instruction was placed in product " + ((s.request || {}).product_id || "") + "'s supplier text.";
      default: return s.summary || "";
    }
  }
  function sideOf(s) { return BUYER[s.kind] ? "buyer" : (GATEWAY[s.kind] ? "gateway" : null); }

  // ---------------------------------------------------------------- frame bodies
  function compareTable(opts) {
    function weighted(o, get) {
      var tot = 0, w = 0;
      (o.offer.lines || []).forEach(function (ln) { var v = get(ln); if (v === null || v === undefined) return;
        var q = parseFloat(ln.line_total_excl_tax) || 0; tot += v * q; w += q; });
      return w ? tot / w : null;
    }
    var wrap = el("div", "cmp-wrap"), t = el("table", "cmp"), head = el("tr");
    head.appendChild(el("th", "", ""));
    opts.forEach(function (o) { head.appendChild(el("th", "", o.label)); });
    t.appendChild(head);
    function line(label, values) {
      if (values.every(function (v) { return v === null || v === "" || v === undefined; })) return;
      var tr = el("tr"); tr.appendChild(el("th", "", label));
      values.forEach(function (v) { tr.appendChild(el("td", "", v === null || v === undefined ? "" : String(v))); });
      t.appendChild(tr);
    }
    line("Total excl. tax", opts.map(function (o) { return money(o.offer.totals.excl_tax) + " EUR"; }));
    line("Products", opts.map(function (o) { return (o.offer.lines || []).map(function (ln) { return ln.quantity.value + " x " + ln.product_id; }).join(", "); }));
    line("Delivery reliability", opts.map(function (o) {
      var v = weighted(o, function (ln) { return (ln.comparison || {}).delivery_reliability; });
      var part = (o.offer.lines || []).some(function (ln) { var c = ln.comparison || {}; return c.delivery_reliability === undefined || c.delivery_reliability === null; });
      return v === null ? null : Math.round(v * 100) + "% delivered" + (part ? " (some products only)" : "");
    }));
    line("Labels", opts.map(function (o) { var set = {};
      (o.offer.lines || []).forEach(function (ln) { ((ln.attributes || {}).labels || []).forEach(function (l) { set[l] = 1; }); });
      return Object.keys(set).map(function (l) { return l.toLowerCase().replace(/_/g, " "); }).join(", "); }));
    line("Carbon (group average)", opts.map(function (o) {
      var v = weighted(o, function (ln) { return (ln.comparison || {}).carbon_kg_co2e_per_kg; });
      return v === null ? null : v.toFixed(2) + " kg CO2e per kg"; }));
    line("Score", opts.map(function (o) { return o.score ? o.score.total.toFixed(2) : null; }));
    wrap.appendChild(t);
    return wrap;
  }
  function verifyTool(jws) {
    var wrap = el("div", "vtool"), a = el("button", "", "Verify in this browser"), b = el("button", "", "Edit the price, verify again");
    a.type = b.type = "button";
    var out = el("span", "vres", "");
    function show(p, label) {
      out.className = "vres"; out.textContent = "Checking...";
      p.then(function (ok) { out.className = "vres " + (ok ? "ok" : "bad"); out.textContent = label + (ok ? ": signature valid" : ": signature INVALID"); })
       .catch(function () { out.className = "vres bad"; out.textContent = "This browser cannot check Ed25519 signatures."; });
    }
    a.addEventListener("click", function () { show(verifyJws(jws), "Signed offer"); });
    b.addEventListener("click", function () { var t = editPrice(jws); show(verifyJws(t.jws), "Price changed to " + t.price); });
    wrap.appendChild(a); wrap.appendChild(b); wrap.appendChild(out);
    return wrap;
  }
  function body(s) {
    var f = document.createDocumentFragment(), d = s.detail || {};
    if (s.kind === "user") return f;
    if (s.kind === "text") {
      var full = String(s.summary || "").trim(), first = firstSentence(full);
      var rest = full.indexOf(first) === 0 ? full.slice(first.length).trim() : full;
      if (rest) f.appendChild(el("p", "say", rest));
      return f;
    }
    if (s.kind === "call") { f.appendChild(el("p", "tool", s.title)); f.appendChild(json((s.request || {}).input || {})); return f; }
    if (s.kind === "alter") {
      f.appendChild(el("p", "note", "Asked for by the agent:")); f.appendChild(json(s.request || {}));
      f.appendChild(el("p", "note", "Sent to Handshake instead:")); f.appendChild(json(s.detail || {}));
      f.appendChild(el("p", "note", "The signed offer states the quantity quoted, so the agent could see it did not match its request."));
      return f;
    }
    if (s.kind === "inject") {
      f.appendChild(el("p", "tool", d.injected || "")); f.appendChild(el("p", "note", "Supplier text as served to the agent:"));
      f.appendChild(el("pre", "", d.served_to_agent || "")); return f;
    }
    if (s.kind === "probe") { f.appendChild(el("p", "say", s.summary)); if (d.evidence) f.appendChild(json(d.evidence)); return f; }
    if (s.kind === "verify") { if ((s.request || {}).jws) f.appendChild(verifyTool(s.request.jws)); f.appendChild(json(d)); return f; }
    if (s.kind === "result" && s.title === "request_offer" && d.options && d.options.length > 1) f.appendChild(compareTable(d.options));
    f.appendChild(json(d));
    return f;
  }
  function fill(frame, s, label, emptyText) {
    frame.textContent = "";
    frame.classList.remove("current", "bad");
    if (!s) { frame.appendChild(el("p", "frame-empty", emptyText)); return; }
    var idx = steps.indexOf(s);
    var w = el("div", "who"); w.appendChild(el("span", "", label)); w.appendChild(el("span", "", "Step " + (idx + 1)));
    frame.appendChild(w);
    frame.appendChild(el("p", "summary", summarize(s)));
    frame.appendChild(body(s));
    if (idx === cursor) frame.classList.add("current");
    if (s.status === "bad" && s.kind !== "alter" && s.kind !== "inject") frame.classList.add("bad");
  }

  // ---------------------------------------------------------------- playback
  function visible() { return steps; }
  function render() {
    var n = steps.length;
    var lastB = null, lastG = null;
    for (var i = 0; i <= cursor && i < n; i++) {
      var sd = sideOf(steps[i]);
      if (sd === "buyer") lastB = steps[i]; else if (sd === "gateway") lastG = steps[i];
    }
    var bLabel = lastB && lastB.kind === "user" ? "You" : (lastB && /scripted/.test(lastB.title || "") ? "External buyer agent (scripted)" : "External buyer agent");
    fill(frameBuyer, lastB, bLabel, "Your agent's latest message appears here.");
    fill(frameGw, lastG, lastG && lastG.kind === "seller" ? "Valio Aimo selling agent (initiator)" : "Handshake", "Handshake's latest answer appears here.");
    pb.count.textContent = n ? "Step " + (cursor + 1) + " of " + n : "No steps yet";
    pb.prev.disabled = cursor <= 0;
    pb.next.disabled = cursor >= n - 1;
    pb.play.disabled = n < 2;
    pb.jump.hidden = following || cursor >= n - 1;
    if (!logEl.hidden) renderLog();
    if (verifyWhenReady) {
      var vb = frameGw.querySelector(".vtool button");
      if (vb) { verifyWhenReady = false; vb.click(); }
    }
  }
  function setCursor(i, follow) {
    cursor = Math.max(0, Math.min(steps.length - 1, i));
    following = !!follow;
    render();
  }
  function stopPlay() { if (playing) { clearInterval(playing); playing = null; pb.play.textContent = "Play"; } }
  pb.prev.addEventListener("click", function () { stopPlay(); setCursor(cursor - 1, false); });
  pb.next.addEventListener("click", function () { stopPlay(); setCursor(cursor + 1, cursor + 1 >= steps.length - 1 && running); });
  pb.jump.addEventListener("click", function () { stopPlay(); setCursor(steps.length - 1, true); });
  pb.play.addEventListener("click", function () {
    if (playing) { stopPlay(); return; }
    if (cursor >= steps.length - 1) cursor = -1;
    following = false;
    pb.play.textContent = "Pause";
    var ms = 1600 / parseFloat(pb.speed.value || "1");
    setCursor(cursor + 1, false);
    playing = setInterval(function () {
      if (cursor >= steps.length - 1) { if (!running) stopPlay(); else following = true; return; }
      setCursor(cursor + 1, false);
    }, ms);
  });
  pb.speed.addEventListener("change", function () { if (playing) { stopPlay(); pb.play.click(); } });
  pb.all.addEventListener("click", function () {
    logEl.hidden = !logEl.hidden;
    pb.all.setAttribute("aria-expanded", logEl.hidden ? "false" : "true");
    pb.all.textContent = logEl.hidden ? "Show all steps" : "Hide all steps";
    if (!logEl.hidden) renderLog();
  });
  function renderLog() {
    logEl.textContent = "";
    steps.forEach(function (s, i) {
      var li = el("li", i === cursor ? "at" : "");
      var h = el("button", "head"); h.type = "button";
      h.appendChild(el("span", "num", String(i + 1)));
      var sd = sideOf(s);
      h.appendChild(el("span", "side-l", sd === "buyer" ? "Buyer agent" : (sd === "gateway" ? (s.kind === "seller" ? "Selling agent" : "Handshake") : "Run")));
      h.appendChild(el("span", "sum", summarize(s)));
      h.addEventListener("click", function () { stopPlay(); setCursor(i, false); });
      li.appendChild(h);
      var payload = s.kind === "call" ? (s.request || {}).input : (s.detail || s.request);
      if (payload && typeof payload === "object") {
        var dt = el("details"); dt.appendChild(el("summary", "", "Data")); dt.appendChild(json(payload)); li.appendChild(dt);
      }
      logEl.appendChild(li);
    });
  }

  // ---------------------------------------------------------------- runs
  function addSteps(list) {
    list.forEach(function (s) {
      if (s.kind === "end") { ended = true; timerStop(tick !== null ? "Run time, no order" : null); setStatus(s.summary, s.status === "bad"); return; }
      if (s.kind === "result" && s.title === "place_order") timerStop("Time to order");
      steps.push(s);
    });
    if (following) cursor = steps.length - 1;
    if (verifyWhenReady) {
      for (var i = 0; i < steps.length; i++) {
        if (steps[i].kind === "verify") { cursor = i; following = false; break; }
      }
    }
    render();
  }
  function showOrder(o, runUrl) {
    var key = o ? o.status + (o.approval_url || "") : "";
    if (key === lastOrder) return;
    lastOrder = key;
    human.textContent = "";
    if (!o) return;
    var c = el("section", "lv-card panel");
    if (o.status === "pending_approval" && o.approval_url) {
      c.appendChild(el("h3", "", "Your turn: approve this order"));
      c.appendChild(el("p", "", "It is over the agent's limits, so a person must approve it. In this demo that person is you."));
      var p = el("p"); var a = el("a", "btn", "Open the approval page");
      a.href = o.approval_url; a.target = "_blank"; a.rel = "noreferrer noopener"; p.appendChild(a); c.appendChild(p);
      if (o.qr) { var q = el("div", "qr"); var img = el("img"); img.src = o.qr; img.alt = "QR code for the approval page"; q.appendChild(img); c.appendChild(q); }
      c.appendChild(el("p", "muted-s", "Waiting for your decision; this page checks every few seconds."));
    } else {
      var titles = { confirmed: "The order is confirmed.", declined: "Declined. Nothing was ordered.",
        expired: "The approval request expired. Nothing was ordered.", rejected: "The gateway rejected this order." };
      if (o.status !== "confirmed") c.className += " stopped";
      c.appendChild(el("h3", "", titles[o.status] || ("Order status: " + o.status)));
      if (o.receipt_url) { var p2 = el("p"); var r2 = el("a", "btn", "Open the signed receipt"); r2.href = o.receipt_url; r2.rel = "noreferrer"; p2.appendChild(r2); c.appendChild(p2); }
    }
    if (runUrl) { var p3 = el("p", "muted-s"); var l = el("a", "", "Open this run as a page"); l.href = runUrl; p3.appendChild(l); c.appendChild(p3); }
    human.appendChild(c);
  }
  function poll() {
    if (!run) return;
    fetch("/demo/live/" + encodeURIComponent(run.trace_id) + "?k=" + encodeURIComponent(run.k) + "&after=" + after, { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 200) { setStatus((res.body && res.body.message) || "The run could not be read.", true); lock(false); return; }
        var b = res.body;
        addSteps(b.steps || []);
        after = b.next;
        showOrder(b.order, b.run_url);
        var pending = b.order && b.order.status === "pending_approval";
        if (b.state === "running") {
          if (!ended) setStatus("Working... " + Math.round((Date.now() - startedAt) / 1000) + " s");
          pollTimer = setTimeout(poll, 1500); return;
        }
        if (b.state === "stopped" && !ended) setStatus("The run stopped unexpectedly. Try again, or use a scripted scenario.", true);
        lock(false);
        if (pending && Date.now() - startedAt < 35 * 60 * 1000) pollTimer = setTimeout(poll, 4000);
      })
      .catch(function () { pollTimer = setTimeout(poll, 4000); });
  }
  function startRun(url, payload, label) {
    clearTimeout(pollTimer); stopPlay();
    var menu = document.getElementById("scenarios");
    if (menu) menu.open = false;
    steps = []; cursor = -1; following = true; after = 0; lastOrder = ""; ended = false;
    human.textContent = "";
    render();
    lock(true);
    setStatus("Starting...");
    post(url, payload).then(function (res) {
      if (res.status !== 202) { setStatus((res.body && res.body.message) || ("HTTP " + res.status), true); lock(false); return; }
      run = res.body; startedAt = Date.now(); timerStart(); setStatus(label); poll();
    }).catch(function () { setStatus("The request did not reach the gateway.", true); lock(false); });
  }
  function behaviour() { var x = form.querySelector('input[name="behaviour"]:checked'); return x ? x.value : "honest"; }
  function startLive(text, beh) {
    startRun("/demo/live", { prompt: text, persona: persona, behaviour: beh }, "Your agent is working...");
  }
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = box.value.trim();
    if (!text) { box.focus(); return; }
    startLive(text, behaviour());
  });

  // ---------------------------------------------------------------- scenario menu
  var GROUPS = [
    ["Ordering", ["hotel_milk", "restaurant_basket", "@prompts"]],
    ["Limits and approval", ["hotel_decline", "restaurant_weekly", "restaurant_weekday", "probation_small", "hospital_unknown", "hotel_twice"]],
    ["Exceptions", ["hotel_split", "hotel_expire", "any_tomorrow", "restaurant_substitutes", "hospital_substitute", "school_milk", "school_no_allergen_data", "school_category", "visitor_search"]],
    ["Comparing offers", ["compare_options"]],
    ["Misbehaving agents", ["misbehaving", "@over_orders", "@injected", "@rogue"]],
    ["Proactive offers", ["proposal_accept", "proposal_not_opted", "proposal_blocked"]],
    ["Trust", ["probation_build", "probation_decline", "conformance"]]
  ];
  function livePrompts() {
    var school = persona === "school";
    return {
      over_orders: ["Over-orders: a faulty client multiplies quantities by 10", school ? "20 kg of bread for Monday" : "20 litres of lactose-free milk for Monday"],
      injected: ["Prompt-injected: a hidden instruction in supplier text", school ? "20 kg of bread for Monday at the best price" : "100 litres of lactose-free milk for Monday at the best price"],
      rogue: ["Misbehaving (rogue) agent with raw API access", school ? "Energy drinks for the sports day, 20 cases" : "I need 1,000 litres of lactose-free milk for Monday."]
    };
  }
  function scenButton(label, kind, live, onClick) {
    var b = el("button", "scen" + (live ? "" : " scripted"));
    b.type = "button";
    b.appendChild(document.createTextNode(label));
    b.appendChild(el("span", "kind", live ? "live model" : "scripted"));
    if (live) b.setAttribute("data-live", "1");
    b.addEventListener("click", function () { if (!b.disabled) onClick(); });
    return b;
  }
  function renderScenarios(sc) {
    scen = sc || { scripted: [], prompts: [] };
    groupsBox.textContent = "";
    var byId = {};
    (scen.scripted || []).forEach(function (x) { byId[x.id] = x; });
    var lp = livePrompts();
    var shown = {};
    GROUPS.forEach(function (g) {
      var ul = el("ul");
      g[1].forEach(function (id) {
        if (id === "@prompts") {
          (scen.prompts || []).forEach(function (t) {
            var li = el("li"); li.appendChild(scenButton(t, "prompt", true, function () { box.value = t; updateCount(); startLive(t, "honest"); })); ul.appendChild(li);
          });
        } else if (id.charAt(0) === "@") {
          if (persona === "visitor") return;
          var k = id.slice(1), spec = lp[k];
          var li2 = el("li"); li2.appendChild(scenButton(spec[0], k, true, function () { box.value = spec[1]; updateCount(); startLive(spec[1], k); })); ul.appendChild(li2);
        } else if (byId[id]) {
          shown[id] = 1;
          var li3 = el("li"); li3.appendChild(scenButton(byId[id].label, "scripted", false, function () {
            startRun("/demo/scenario", { id: id, persona: persona }, "Running the scripted scenario...");
          })); ul.appendChild(li3);
        }
      });
      if (ul.children.length) { var sec = el("div"); sec.appendChild(el("h3", "", g[0])); sec.appendChild(ul); groupsBox.appendChild(sec); }
    });
    var rest = (scen.scripted || []).filter(function (x) { return !shown[x.id]; });
    if (rest.length) {
      var ul2 = el("ul");
      rest.forEach(function (x) { var li = el("li"); li.appendChild(scenButton(x.label, "scripted", false, function () {
        startRun("/demo/scenario", { id: x.id, persona: persona }, "Running the scripted scenario..."); })); ul2.appendChild(li); });
      var sec2 = el("div"); sec2.appendChild(el("h3", "", "More")); sec2.appendChild(ul2); groupsBox.appendChild(sec2);
    }
    lock(running);
  }

  // ---------------------------------------------------------------- personas, rules and edit controls
  function renderRules(c, sandbox) {
    card = c;
    rulesWho.textContent = c.customer ? c.label + " for " + c.customer + ". Set by the customer's purchasing manager (you, in this demo)." : c.blurb;
    rulesList.textContent = "";
    (c.rules || []).forEach(function (r) {
      var v = String(r.value);
      rulesList.appendChild(el("li", "", r.name + ": " + v + (/[.)]$/.test(v) ? "" : ".")));
    });
    if (!c.rules) rulesList.appendChild(el("li", "", c.blurb));
    editBtn.hidden = !c.rules;
    editBox.textContent = "";
    if (c.rules) buildEdit(c);
    if (sandbox && sandbox.expires_at) document.getElementById("sandbox-note").textContent =
      "Your private sandbox (its own copy of every customer, agent and limits) lasts until " + hm(sandbox.expires_at) + ". No sign-in.";
  }
  editBtn.addEventListener("click", function () {
    editBox.hidden = !editBox.hidden;
    editBtn.textContent = editBox.hidden ? "Edit" : "Done";
  });
  function buildEdit(c) {
    var msg = el("span", "msg", "");
    function after(res, text) {
      if (res.status === 200) { renderRules(res.body.card, res.body.sandbox); editBox.hidden = false; editBtn.textContent = "Done";
        var m = editBox.querySelector(".msg"); if (m) m.textContent = text.replace("{v}", res.body.card.mandate_version); }
      else { msg.className = "msg bad"; msg.textContent = (res.body && res.body.message) || "Not saved."; }
    }
    if (!c.ladder) {
      var f = el("form"); var lab = el("label", "", "Per-order cap (EUR)");
      var inp = el("input"); inp.type = "number"; inp.step = "0.01"; inp.min = "0.01"; inp.max = "100000"; inp.value = c.cap || ""; inp.id = "cap-input";
      lab.setAttribute("for", "cap-input");
      var save = el("button", "save", "Save cap"); save.type = "submit";
      f.appendChild(lab); f.appendChild(inp); f.appendChild(save);
      f.addEventListener("submit", function (ev) { ev.preventDefault();
        post("/demo/sandbox/cap", { persona: persona, cap: inp.value }).then(function (res) { after(res, "Saved as mandate version {v}. Run your agent again."); }); });
      editBox.appendChild(f);
    }
    if (c.preferences) {
      var pr = el("div", "row"); pr.appendChild(el("span", "hh", "Rank offers by"));
      c.preferences.forEach(function (p) {
        var b = el("button", "pref", p.label); b.type = "button";
        b.setAttribute("aria-pressed", c.preference === p.id ? "true" : "false");
        b.addEventListener("click", function () { post("/demo/sandbox/preference", { persona: persona, preference: p.id })
          .then(function (res) { after(res, "Ranking set (mandate version {v})."); }); });
        pr.appendChild(b);
      });
      editBox.appendChild(pr);
    }
    if (c.proactive) {
      var pa = el("div"); pa.appendChild(el("span", "hh", "Proactive offers from Valio Aimo"));
      var row1 = el("div", "row"); var onL = el("label"); var on = el("input"); on.type = "checkbox"; on.checked = !!c.proactive.enabled;
      onL.appendChild(on); onL.appendChild(document.createTextNode(" Allow")); row1.appendChild(onL);
      c.proactive.choices.forEach(function (ch) { var l = el("label"); var cb = el("input"); cb.type = "checkbox"; cb.value = ch.code;
        cb.checked = (c.proactive.categories || []).indexOf(ch.code) >= 0; cb.className = "pa-cat";
        l.appendChild(cb); l.appendChild(document.createTextNode(" " + ch.name)); row1.appendChild(l); });
      pa.appendChild(row1);
      var row2 = el("div", "row"); var ml = el("label", "", "At most "); var mx = el("input"); mx.type = "number"; mx.min = "0"; mx.max = "20"; mx.value = c.proactive.max_per_week;
      ml.appendChild(mx); ml.appendChild(document.createTextNode(" per week")); row2.appendChild(ml);
      var ps = el("button", "save", "Save consent"); ps.type = "button";
      ps.addEventListener("click", function () {
        var chosen = Array.prototype.map.call(pa.querySelectorAll(".pa-cat:checked"), function (x) { return x.value; });
        post("/demo/sandbox/proactive", { persona: persona, enabled: on.checked, categories: chosen, max_per_week: parseInt(mx.value || "0", 10) })
          .then(function (res) { after(res, "Consent saved (mandate version {v})."); });
      });
      row2.appendChild(ps); pa.appendChild(row2);
      editBox.appendChild(pa);
    }
    editBox.appendChild(msg);
  }
  function choose(slug) {
    persona = slug;
    try { sessionStorage.setItem("handshake.persona", slug); } catch (e) { /* private mode */ }
    document.querySelectorAll(".pb[data-persona]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-persona") === slug ? "true" : "false");
    });
    var req = sandboxQueue.then(function () { return fetch("/demo/sandbox?persona=" + encodeURIComponent(slug), { credentials: "same-origin" }); });
    sandboxQueue = req.catch(function () { /* keep going */ });
    return req.then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status !== 200) { rulesList.textContent = ""; rulesList.appendChild(el("li", "", res.body.message || "The sandbox could not be loaded.")); return; }
        var role = slug === "approver" || slug === "operations";
        form.hidden = role;
        rolePanel.hidden = !role;
        if (slug === "approver") { rulesWho.textContent = "Approver"; rulesList.textContent = ""; rulesList.appendChild(el("li", "", "You are the customers' purchasing manager. Orders over a mandate's limits wait for your decision.")); editBtn.hidden = true; editBox.textContent = ""; renderInbox(res.body.inbox || []); return; }
        if (slug === "operations") { rulesWho.textContent = "Valio Aimo operations"; rulesList.textContent = ""; rulesList.appendChild(el("li", "", "What Valio Aimo sees about the agents in your sandbox, with a suspend switch, a log-tamper control and proactive offers.")); editBtn.hidden = true; editBox.textContent = ""; renderOps(res.body.ops || {}); return; }
        renderRules(res.body.card, res.body.sandbox);
        renderScenarios(res.body.scenarios);
      })
      .catch(function () { rulesList.textContent = ""; rulesList.appendChild(el("li", "", "The sandbox could not be loaded.")); });
  }
  document.querySelectorAll(".pb[data-persona]").forEach(function (b) {
    b.addEventListener("click", function () { if (!running) choose(b.getAttribute("data-persona")); });
  });

  // ---------------------------------------------------------------- Approver and operations roles
  function renderInbox(items) {
    rolePanel.textContent = "";
    rolePanel.appendChild(el("p", "hh", "Approval requests in your sandbox"));
    if (!items.length) { rolePanel.appendChild(el("p", "", "Nothing is waiting. Run a scenario that goes over a limit, such as the Hotel chain's example milk order, then come back.")); return; }
    items.forEach(function (it) {
      var d = el("div", "item");
      d.appendChild(el("h4", "", it.customer + ": " + it.total_excl_tax + " EUR excl. tax"));
      (it.reasons || []).forEach(function (r) { d.appendChild(el("p", "", r)); });
      d.appendChild(el("p", "", "Expires " + hm(it.expires_at) + "."));
      var acts = el("div", "acts");
      [["approve", "Approve", ""], ["decline", "Decline", "no"]].forEach(function (a) {
        var b = el("button", a[2], a[1]); b.type = "button";
        b.addEventListener("click", function () {
          acts.querySelectorAll("button").forEach(function (x) { x.disabled = true; });
          fetch(it.approve_path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ decision: a[0] }).toString() }).then(function () { choose("approver"); }).catch(function () { choose("approver"); });
        });
        acts.appendChild(b);
      });
      d.appendChild(acts); rolePanel.appendChild(d);
    });
  }
  var lastSendNote = "";
  function renderOps(o) {
    rolePanel.textContent = "";
    var agents = o.agents || [];
    rolePanel.appendChild(el("p", "hh", "Agents in your sandbox"));
    if (!agents.length) rolePanel.appendChild(el("p", "", "No agents yet: run a scenario as a buyer first."));
    agents.forEach(function (a) {
      var d = el("div", "item" + (a.status === "suspended" ? " off" : ""));
      d.appendChild(el("h4", "", a.name)); d.appendChild(el("p", "", (a.persona_label || "") + " for " + a.customer + ": " + a.status));
      var acts = el("div", "acts");
      var b = el("button", a.status === "suspended" ? "" : "no", a.status === "suspended" ? "Reinstate" : "Suspend"); b.type = "button";
      b.addEventListener("click", function () { b.disabled = true;
        post("/demo/ops/suspend", { agent_id: a.agent_id, suspended: a.status !== "suspended" }).then(function (res) { if (res.status === 200) renderOps(res.body); else b.disabled = false; }); });
      acts.appendChild(b);
      if (a.status === "suspended" && a.persona) {
        var t = el("button", "", "Show it being refused"); t.type = "button";
        t.addEventListener("click", function () { startRun("/demo/scenario", { id: "suspended_probe", persona: a.persona }, "The suspended agent tries to order..."); });
        acts.appendChild(t);
      }
      d.appendChild(acts); rolePanel.appendChild(d);
    });
    var buyers = agents.filter(function (a) { return a.persona; });
    if (buyers.length) {
      rolePanel.appendChild(el("p", "hh", "Send seasonal offer (Valio Aimo selling agent, scripted)"));
      var row = el("div", "acts"); var sel = el("select", "pa-select"); sel.setAttribute("aria-label", "Buyer");
      buyers.forEach(function (a) { var op = el("option", "", a.persona_label + ": " + a.customer); op.value = a.agent_id; sel.appendChild(op); });
      var sb = el("button", "", "Send seasonal offer"); sb.type = "button";
      var note = el("p", "", lastSendNote);
      sb.addEventListener("click", function () { sb.disabled = true;
        post("/demo/ops/propose", { agent_id: sel.value }).then(function (res) { sb.disabled = false; var b2 = res.body || {};
          lastSendNote = res.status !== 200 ? (b2.message || "Not sent.") : (b2.status === "open" ? "Sent: proposal " + b2.proposal_id + " for product " + b2.product_id + ", " + b2.total_excl_tax + " EUR excl. tax, valid 30 minutes." : "Blocked by the gateway: " + b2.reason);
          note.textContent = lastSendNote;
          fetch("/demo/ops", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(renderOps); }); });
      row.appendChild(sel); row.appendChild(sb); rolePanel.appendChild(row); rolePanel.appendChild(note);
    }
    var orders = o.orders || [];
    rolePanel.appendChild(el("p", "hh", "Orders, with the full Valio Aimo receipt view"));
    if (!orders.length) rolePanel.appendChild(el("p", "", "No orders yet."));
    orders.forEach(function (x) {
      var d = el("div", "item");
      d.appendChild(el("h4", "", x.order_id + ": " + x.status + ", " + x.total_excl_tax + " EUR"));
      var acts = el("div", "acts"); var open = el("a", "plain", "Open the Valio Aimo receipt");
      open.href = x.receipt_path; open.target = "_blank"; open.rel = "noreferrer noopener"; acts.appendChild(open);
      var tb = el("button", "no", "Tamper with the log"); tb.type = "button"; var n2 = el("p", "", "");
      tb.addEventListener("click", function () { tb.disabled = true;
        post("/demo/ops/tamper", { order_id: x.order_id }).then(function (res) {
          if (res.status !== 200) { n2.textContent = res.body.message || "Not changed."; tb.disabled = false; return; }
          n2.textContent = "Event " + res.body.seq + " (" + res.body.type + ") now says outcome '" + res.body.changed.outcome.to + "' instead of '" + res.body.changed.outcome.from + "', without updating its hash. Open the receipt: it reports the record as broken."; }); });
      acts.appendChild(tb); d.appendChild(acts); d.appendChild(n2); rolePanel.appendChild(d);
    });
    var props = o.proposals || [];
    if (props.length) {
      rolePanel.appendChild(el("p", "hh", "Proposals"));
      var pl = el("ul", "ops-list");
      props.forEach(function (x) { pl.appendChild(el("li", "", x.proposal_id + ": " + x.status + ", product " + x.product_id + ", " + x.agent +
        (x.blocked_reason ? ". Refused: " + x.blocked_reason.replace(/\.$/, "") : ""))); });
      rolePanel.appendChild(pl);
    }
    var access = o.access || [];
    rolePanel.appendChild(el("p", "hh", "Which agent read which products and prices"));
    var al = el("ul", "ops-list");
    if (!access.length) al.appendChild(el("li", "", "No data access yet."));
    access.forEach(function (x) { al.appendChild(el("li", "", hm(x.at) + "  " + x.agent + ": " + x.text)); });
    rolePanel.appendChild(al);
  }

  // ---------------------------------------------------------------- signature check in the browser
  var jwksCache = null;
  function b64u(s) { s = s.replace(/-/g, "+").replace(/_/g, "/"); while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i); return out; }
  function toB64u(bytes) { var bin = ""; bytes.forEach(function (b) { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
  function verifyJws(jws) {
    var parts = jws.split(".");
    if (parts.length !== 3 || !window.crypto || !crypto.subtle) return Promise.reject(new Error("unsupported"));
    var header = JSON.parse(new TextDecoder().decode(b64u(parts[0])));
    var keys = jwksCache ? Promise.resolve(jwksCache) : fetch("/.well-known/jwks.json").then(function (r) { return r.json(); }).then(function (j) { jwksCache = j; return j; });
    return keys.then(function (set) {
      var jwk = (set.keys || []).filter(function (k) { return k.kid === header.kid; })[0];
      if (!jwk || header.alg !== "EdDSA") return false;
      return crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"])
        .then(function (key) { return crypto.subtle.verify({ name: "Ed25519" }, key, b64u(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1])); });
    });
  }
  function editPrice(jws) {
    var parts = jws.split("."), payload = JSON.parse(new TextDecoder().decode(b64u(parts[1]))), line = (payload.lines || [])[0];
    if (line) line.unit_price_excl_tax = (Math.max(0.01, parseFloat(line.unit_price_excl_tax) - 0.1)).toFixed(2);
    return { jws: parts[0] + "." + toB64u(new TextEncoder().encode(JSON.stringify(payload))) + "." + parts[2], price: line ? line.unit_price_excl_tax : "?" };
  }

  // ---------------------------------------------------------------- start: persona, then a Start here step from the landing page
  var start = null;
  try { start = new URLSearchParams(location.search).get("start"); } catch (e) { start = null; }
  if (start === "example" || start === "verify") persona = "hotel";
  else if (start === "school") persona = "school";
  else if (start === "misbehave" && (persona === "visitor" || persona === "approver" || persona === "operations")) persona = "restaurant";
  if (!document.querySelector('.pb[data-persona="' + persona + '"]')) persona = "hotel";
  choose(persona).then(function () {
    if (start === "example") startRun("/demo/scenario", { id: "hotel_milk", persona: "hotel" }, "Running the scripted scenario...");
    else if (start === "school") startRun("/demo/scenario", { id: "school_milk", persona: "school" }, "Running the scripted scenario...");
    else if (start === "misbehave") startRun("/demo/scenario", { id: "misbehaving", persona: persona }, "The misbehaving agent is trying...");
    else if (start === "verify") {
      verifyWhenReady = true;
      startRun("/demo/scenario", { id: "hotel_milk", persona: "hotel" }, "Running the scripted scenario...");
    }
  });
  updateCount();
  render();
})();
