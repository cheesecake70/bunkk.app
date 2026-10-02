/* BUNKR landing page. Vanilla, no libraries.

   One scroll handler, run through requestAnimationFrame, turns scroll position
   into a handful of CSS custom properties and classes. CSS does the drawing.
   With reduced motion requested the scrolling engine never starts and the page
   stays in its plain, fully readable state (the scene 1 buttons still work).

   What gets written, and where:
     <html>     .lp-js          enables the "before" states in landing.css. A tiny
                                inline script in <head> adds it before first paint;
                                it is added here too, and window.__lpReady tells
                                that script this file arrived.
                .lp-sticky      wide screens: scenes pile into one pinned stage
                .lp-stack       narrow screens: rows that play when they scroll in
                --pp            page scroll progress 0..1 (narrow-screen rail)
     .lp-stage  --p             story progress 0..1
                data-scene      1..5, flips halfway through the hand-off
     .lp-scene  --sp            progress inside the scene 0..1
                --wo            frame wipe-out 0..1 over the scene's last 20%
                --co            outgoing copy leaving 0..1  (first half of --wo)
                --ci            incoming copy arriving 0..1 (second half of the
                                previous scene's --wo)
                .is-s1..is-s4   cumulative steps reached at fixed --sp values
                .is-wiping      while 0 < --wo < 1 (shows the squeegee bar)
                .is-gone        once --wo reaches 1 (visibility:hidden)
     .lp-rail__seg  --f         how finished that scene is, 0..1 */
(function () {
  "use strict";

  var root = document.documentElement;

  function clamp(n, lo, hi) { return n < lo ? lo : (n > hi ? hi : n); }
  function $all(sel, from) { return Array.prototype.slice.call((from || document).querySelectorAll(sel)); }
  function restart(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;              // let the animation start over
    el.classList.add(cls);
  }

  var scenes = $all(".lp-scene");

  /* ---- Scene 1: you can press it ------------------------------------------
     Each row carries data-base (how many it could still spare before this
     click). Skipping spends one: the badge shows base-1 and its colour follows
     the number. The verdict card above restamps. The scripted press of the last
     row at step 1 stops the moment you have clicked anything yourself.
     These buttons are real (named, focusable); the rest of the page is not. */
  var play = (function () {
    var scene = scenes[0];
    if (!scene) return null;
    var rows = $all(".lp-row", scene);
    var dayBtn = scene.querySelector(".lp-daybtn");
    var card = document.getElementById("lp-s1-card");
    var word = document.getElementById("lp-s1-word");
    var reason = document.getElementById("lp-s1-reason");
    var scripted = rows[rows.length - 1];
    var touched = false;

    function isOn(row) { return row.getAttribute("data-on") === "1"; }

    function setRow(row, on, animate) {
      var left = parseInt(row.getAttribute("data-base"), 10) - (on ? 1 : 0);
      var btn = row.querySelector(".lp-skipbtn");
      var badge = row.querySelector(".lp-left");
      row.setAttribute("data-on", on ? "1" : "0");
      row.classList.toggle("is-planned", on);
      btn.classList.toggle("is-on", on);
      btn.classList.toggle("btn--danger", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      badge.textContent = left + " left";
      badge.className = "badge lp-left badge--" + (left <= 0 ? "danger" : (left === 1 ? "warn" : "safe"));
      if (on && animate) restart(btn, "is-anim");
    }

    function summarise(restamp) {
      var on = rows.filter(isOn);
      var dry = on.filter(function (r) { return parseInt(r.getAttribute("data-base"), 10) - 1 <= 0; });
      var codes = function (list) { return list.map(function (r) { return r.getAttribute("data-code"); }); };
      var text;
      if (!on.length) {
        text = "CS201, EE110 — all within budget.";
      } else {
        text = "Already planning to miss " + codes(on).join(", ") + ".";
        if (dry.length) {
          text += " " + codes(dry).join(", ") + (dry.length === 1 ? " has" : " have") +
            " no room left after this.";
        }
      }
      reason.textContent = text;
      var all = on.length === rows.length;
      dayBtn.classList.toggle("is-on", all);
      dayBtn.classList.toggle("btn--danger", all);
      dayBtn.setAttribute("aria-pressed", all ? "true" : "false");
      dayBtn.textContent = all ? "Skipping all day" : "Skip whole day";
      if (restamp) { restart(word, "is-flap"); restart(card, "is-thud"); }
    }

    scene.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest(".lp-skipbtn, .lp-daybtn") : null;
      if (!btn) return;
      touched = true;
      if (btn.classList.contains("lp-daybtn")) {
        var turnOn = !rows.every(isOn);
        rows.forEach(function (r) { setRow(r, turnOn, turnOn); });
      } else {
        var row = btn.closest(".lp-row");
        setRow(row, !isOn(row), true);
      }
      summarise(true);
    });

    return {
      /* Called by the scroll engine. Does nothing once you have taken over. */
      script: function (step) {
        if (touched) return;
        var on = step >= 1;
        if (isOn(scripted) === on) return;
        setRow(scripted, on, on);
        summarise(on);
      }
    };
  })();

  /* ---- Reduced motion: nothing below runs ------------------------------------ */
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (reduce.matches) return;

  root.classList.add("lp-js");

  // Steps land through the scene and keep landing until the hand-off begins.
  var STEPS = [0.08, 0.27, 0.46, 0.65];
  var WIPE_AT = 0.80;                // --sp where the hand-off window opens
  var COUNT_BY = 0.60;               // --sp by which the scrubbed meter and counts land

  var story = document.querySelector(".lp-story");
  var stage = document.querySelector(".lp-stage");
  var segs = $all(".lp-rail__seg");
  var wide = window.matchMedia("(min-width: 900px)");
  var sticky = false;
  var ticking = false;
  var reveals;
  var cache = scenes.map(function () { return { sp: -1, wo: -1, co: -1, ci: -1, step: -1, f: -1 }; });
  var lastPP = -1, lastP = -1, lastScene = 0, lastGone = [];

  function setStep(scene, step) {
    for (var k = 1; k <= STEPS.length; k++) scene.classList.toggle("is-s" + k, step >= k);
    // The tick in the day sheet is a real checkbox, so it is set, not styled.
    $all("[data-lp-check]", scene).forEach(function (box) { box.checked = step >= 4; });
    if (scene === scenes[0] && play) play.script(step);
  }

  function setCounts(scene, k) {
    $all("[data-count]", scene).forEach(function (el) {
      var next = String(Math.round(parseFloat(el.getAttribute("data-count")) * k));
      if (el.textContent !== next) el.textContent = next;
    });
  }

  function prop(el, name, value, digits) { el.style.setProperty(name, value.toFixed(digits || 4)); }

  function update() {
    ticking = false;
    var vh = window.innerHeight;
    var room = root.scrollHeight - vh;
    var pp = room > 0 ? clamp(window.pageYOffset / room, 0, 1) : 0;
    if (Math.abs(pp - lastPP) > 0.0005) {
      lastPP = pp;
      root.style.setProperty("--pp", pp.toFixed(4));
    }
    checkReveals();
    if (!sticky) checkStack();
    if (!sticky || !story) return;

    var box = story.getBoundingClientRect();
    var span = box.height - vh;
    var p = span > 0 ? clamp(-box.top / span, 0, 1) : 0;
    if (Math.abs(p - lastP) > 0.0005) {
      lastP = p;
      prop(stage, "--p", p);
    }

    var n = scenes.length;
    var s = p * n;
    // The counter and the block behind the frame change at the midpoint of the
    // hand-off, when no copy is on screen.
    var mid = (1 - WIPE_AT) / 2;
    var current = Math.min(n, Math.floor(s + mid) + 1);
    if (current !== lastScene) {
      lastScene = current;
      stage.setAttribute("data-scene", String(current));
      segs.forEach(function (seg, i) {
        if (i + 1 === current) seg.setAttribute("aria-current", "true");
        else seg.removeAttribute("aria-current");
      });
    }

    var prevWo = 1;
    scenes.forEach(function (scene, i) {
      var sp = clamp(s - i, 0, 1);
      var wo = i < n - 1 ? clamp((sp - WIPE_AT) / (1 - WIPE_AT), 0, 1) : 0;
      var co = clamp(wo * 2, 0, 1);
      var ci = i === 0 ? 1 : clamp((prevWo - 0.5) * 2, 0, 1);
      prevWo = wo;
      var step = 0;
      for (var k = 0; k < STEPS.length; k++) if (sp >= STEPS[k]) step = k + 1;
      var c = cache[i];

      if (Math.abs(sp - c.sp) > 0.0005) {
        c.sp = sp;
        prop(scene, "--sp", sp);
        if (i === 1) setCounts(scene, clamp(sp / COUNT_BY, 0, 1));
        if (segs[i]) prop(segs[i], "--f", sp, 3);
      }
      if (Math.abs(wo - c.wo) > 0.001) {
        c.wo = wo;
        prop(scene, "--wo", wo);
        scene.classList.toggle("is-wiping", wo > 0 && wo < 1);
        scene.classList.toggle("is-gone", wo >= 1);
      }
      if (Math.abs(co - c.co) > 0.001) { c.co = co; prop(scene, "--co", co); }
      if (Math.abs(ci - c.ci) > 0.001) { c.ci = ci; prop(scene, "--ci", ci); }
      if (step !== c.step) {
        c.step = step;
        setStep(scene, step);
      }
    });
  }

  function request() {
    if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
  }

  /* Rail: jump to the start of a scene inside the pinned track. */
  segs.forEach(function (seg, i) {
    seg.addEventListener("click", function (e) {
      if (!sticky || !story) return;
      e.preventDefault();
      var top = story.getBoundingClientRect().top + window.pageYOffset;
      var span = story.offsetHeight - window.innerHeight;
      window.scrollTo({ top: top + span * (i + 0.02) / scenes.length, behavior: "smooth" });
    });
  });

  /* Narrow screens: each scene plays once, step by step, when its frame
     comes into view. */
  var timers = [];
  function countUp(scene) {
    var started = null;
    function frame(now) {
      if (started === null) started = now;
      var k = clamp((now - started) / 900, 0, 1);
      setCounts(scene, k);
      if (k < 1) window.requestAnimationFrame(frame);
    }
    window.requestAnimationFrame(frame);
  }
  function playScene(scene) {
    if (scene._lpPlayed) return;
    scene._lpPlayed = true;
    STEPS.forEach(function (_, i) {
      timers.push(window.setTimeout(function () {
        setStep(scene, i + 1);
        if (i === 0 && scene.getAttribute("data-scene") === "2") countUp(scene);
      }, 250 + i * 650));
    });
  }
  /* Checked from the scroll handler, like the reveals: a scene that never
     gets its cue would keep its "before" state for good. */
  function checkStack() {
    var line = window.innerHeight * 0.7;
    scenes.forEach(function (scene) {
      if (scene._lpPlayed) return;
      var frame = scene.querySelector(".lp-frame");
      if (!frame) return;
      var box = frame.getBoundingClientRect();
      if (box.top < line && box.bottom > 0) playScene(scene);
    });
  }

  function setMode() {
    sticky = wide.matches && !!story && !!stage;
    root.classList.toggle("lp-sticky", sticky);
    root.classList.toggle("lp-stack", !sticky);
    timers.forEach(window.clearTimeout); timers = [];
    scenes.forEach(function (scene, i) {
      cache[i] = { sp: -1, wo: -1, co: -1, ci: -1, step: -1, f: -1 };
      scene._lpPlayed = false;
      ["--sp", "--wo", "--co", "--ci"].forEach(function (name) { scene.style.removeProperty(name); });
      scene.classList.remove("is-wiping", "is-gone");
      setStep(scene, 0);
    });
    if (sticky) { lastScene = 0; update(); }
    else { if (stage) stage.setAttribute("data-scene", "1"); checkStack(); }
  }

  /* ---- One-shot reveals ------------------------------------------------------
     Checked in the scroll handler rather than with an IntersectionObserver: the
     hidden state is a clip-path, and a fully clipped box can count as "not
     intersecting", which would keep it hidden forever. */
  reveals = $all("[data-lp-reveal]");
  function checkReveals() {
    if (!reveals || !reveals.length) return;
    var line = window.innerHeight * 0.9;
    reveals = reveals.filter(function (el) {
      if (el.getBoundingClientRect().top > line) return true;
      el.classList.add("is-in");
      // Lets go of the clip once it has opened, so hover shadows can't be cut.
      window.setTimeout(function () { el.classList.add("is-done"); }, 560);
      return false;
    });
  }

  if (wide.addEventListener) wide.addEventListener("change", setMode);
  else if (wide.addListener) wide.addListener(setMode);
  window.addEventListener("scroll", request, { passive: true });
  window.addEventListener("resize", request, { passive: true });
  setMode();
  request();

  /* ---- Pointer: which way is the light? ----------------------------------- */
  function pointerVars(target, host, e) {
    var r = host.getBoundingClientRect();
    var x = clamp(((e.clientX - r.left) / r.width - 0.5) * 2, -1, 1);
    var y = clamp(((e.clientY - r.top) / r.height - 0.5) * 2, -1, 1);
    target.style.setProperty("--tx", x.toFixed(3));
    target.style.setProperty("--ty", y.toFixed(3));
  }
  function track(host, target) {
    var pending = null;
    host.addEventListener("pointermove", function (e) {
      if (e.pointerType === "touch") return;
      pending = e;
      window.requestAnimationFrame(function () {
        if (pending) { pointerVars(target, host, pending); pending = null; }
      });
    }, { passive: true });
    host.addEventListener("pointerleave", function () {
      pending = null;
      target.style.setProperty("--tx", "0");
      target.style.setProperty("--ty", "0");
    });
  }

  var hero = document.querySelector(".lp-hero");
  var heroStage = document.querySelector("[data-lp-hero]");
  if (hero && heroStage) track(hero, heroStage);
  $all("[data-lp-tilt]").forEach(function (card) { track(card, card); });

  /* ---- Hero verdict: it keeps changing its mind ---------------------------- */
  var hv = document.getElementById("lp-hv");
  var word = document.getElementById("lp-hv-word");
  var reason = document.getElementById("lp-hv-reason");
  var VERDICTS = [
    { cls: "skip",    word: "SKIP",            reason: "CS201, EE110 — all within budget." },
    { cls: "partial", word: "PART SKIP",       reason: "Leave after 12:00 — skips CS201. 1h free." },
    { cls: "go",      word: "CAN’T SKIP", reason: "No room left in MA102." }
  ];
  if (hv && word && reason) {
    var at = 0, cycle = null, onScreen = true;
    var show = function (i) {
      var v = VERDICTS[i];
      hv.className = hv.className.replace(/hero-verdict--(skip|partial|go)/, "hero-verdict--" + v.cls);
      word.textContent = v.word;
      reason.textContent = v.reason;
      restart(word, "is-flap");
      restart(hv, "is-thud");
    };
    var tick = function () { at = (at + 1) % VERDICTS.length; show(at); };
    var sync = function () {
      var run = onScreen && !document.hidden;
      if (run && !cycle) cycle = window.setInterval(tick, 2800);
      if (!run && cycle) { window.clearInterval(cycle); cycle = null; }
    };
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        onScreen = entries[0].isIntersecting; sync();
      }, { threshold: 0.2 }).observe(hv);
    }
    document.addEventListener("visibilitychange", sync);
    sync();
  }

  checkReveals();
  window.__lpReady = true;      // tells the <head> snippet we loaded and ran
})();
