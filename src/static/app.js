/* Progressive enhancement only -- every action here also works as a plain
   form POST if JS is off. */
(function () {
  "use strict";

  var MARK_LABELS = {
    pending: "Archiving\u2026",
    ok: "Archived",
    failed: "Archive failed",
    thin: "Partial archive"
  };
  var MARK_VIA = {
    "archive.today": " via archive.today",
    browser: " via your browser"
  };

  function post(url, data) {
    var body = new FormData();
    Object.keys(data || {}).forEach(function (key) { body.append(key, data[key]); });
    return fetch(url, {
      method: "POST",
      body: body,
      headers: { "X-Requested-With": "XMLHttpRequest" }
    }).then(function (resp) { return resp.json(); });
  }

  function row(el) { return el.closest(".article"); }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-action]");
    if (!trigger) return;
    var item = row(trigger);
    if (!item) return;
    var id = item.dataset.id;

    /* Two controls, one state: the seal and the text button in the corner. */
    if (trigger.dataset.action === "toggle") {
      event.preventDefault();
      post("/a/" + id + "/status", {}).then(function (data) {
        var isRead = data.status === "read";
        item.classList.toggle("is-read", isRead);
        var label = "Mark " + (isRead ? "unread" : "read");

        var seal = item.querySelector(".toggle");
        if (seal) {
          seal.title = label;
          seal.setAttribute("aria-label", label);
        }
        var text = item.querySelector('[data-role="status-label"]');
        if (text) {
          text.textContent = "mark " + (isRead ? "unread" : "read");
          var field = text.form && text.form.querySelector('[name="status"]');
          if (field) field.value = isRead ? "unread" : "read";
        }
      });
    }

    if (trigger.dataset.action === "delete") {
      if (!confirm("Delete this article and its archive?")) return;
      post("/a/" + id + "/delete", {}).then(function () {
        /* Take the month's heading with it if it was the last one there. */
        var before = item.previousElementSibling;
        var after = item.nextElementSibling;
        item.remove();
        if (before && before.classList.contains("month-break")
            && !(after && after.classList.contains("article"))) {
          before.remove();
        }
      });
    }

    if (trigger.dataset.action === "add-tag") {
      showTagInput(item, trigger, id);
    }

    if (trigger.dataset.action === "accept-suggestion") {
      event.preventDefault();
      var name = trigger.dataset.tag;
      trigger.disabled = true;
      post("/a/" + id + "/suggestions/accept", { tag: name })
        .then(function (data) {
          trigger.remove();
          renderTags(item, data.tags);
        })
        .catch(function () { trigger.disabled = false; });
    }

    /* Marking is staged: nothing leaves until dismiss is pressed, so a
       misclick costs a second click rather than a tag. */
    if (trigger.dataset.action === "toggle-remove") {
      event.preventDefault();
      trigger.classList.toggle("chip--removing");
    }

    /* One button, two jobs: delete what you marked, drop what you ignored. */
    if (trigger.dataset.action === "commit-removals") {
      event.preventDefault();
      var marked = Array.prototype.map.call(
        item.querySelectorAll(".chip--removing"),
        function (chip) { return chip.dataset.tag; }
      );
      var pending = item.querySelectorAll(".chip--suggest").length;
      if (!marked.length && !pending) return;

      trigger.disabled = true;
      post("/a/" + id + "/tags", { remove: marked.join(","), dismiss: "1" })
        .then(function (data) {
          item.querySelectorAll(".chip--suggest").forEach(function (chip) {
            chip.remove();
          });
          renderTags(item, data.tags);
          trigger.disabled = false;
        })
        .catch(function () { trigger.disabled = false; });
    }
  });

  function showTagInput(item, trigger, id) {
    if (item.querySelector(".tag-input")) return;
    var input = document.createElement("input");
    input.className = "tag-input chip--sm";
    input.placeholder = "tag";
    input.size = 10;
    trigger.before(input);
    input.focus();

    function finish(save) {
      var value = input.value.trim();
      input.remove();
      if (!save || !value) return;
      post("/a/" + id + "/tags", { add: value }).then(function (data) {
        renderTags(item, data.tags);
      });
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") finish(false);
    });
    input.addEventListener("blur", function () { finish(true); });
  }

  function renderTags(item, tags) {
    var form = item.querySelector('[data-role="tag-form"]');
    /* Anything still marked stays marked across a re-render. */
    var marked = {};
    form.querySelectorAll(".chip--removing").forEach(function (chip) {
      marked[chip.dataset.tag] = true;
    });

    form.querySelectorAll('[data-action="toggle-remove"]').forEach(
      function (chip) { chip.remove(); }
    );
    tags.forEach(function (name) {
      var chip = document.createElement("button");
      chip.type = "submit";
      chip.name = "remove";
      chip.value = name;
      chip.className = "chip chip--sm" + (marked[name] ? " chip--removing" : "");
      chip.dataset.action = "toggle-remove";
      chip.dataset.tag = name;
      chip.title = "Click to mark for removal";
      chip.textContent = name;
      form.appendChild(chip);
    });
  }

  /* Rows added a moment ago are still being fetched; refresh them in place
     until the archiver is done with all of them. */
  var pollTimer = null;

  function pendingIds() {
    return Array.prototype.map.call(
      document.querySelectorAll('.article[data-archive="pending"]'),
      function (el) { return el.dataset.id; }
    );
  }

  function poll() {
    var ids = pendingIds();
    if (!ids.length) { pollTimer = null; return; }

    fetch("/api/articles?ids=" + ids.join(","), {
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        var stillWaiting = false;
        data.articles.forEach(function (article) {
          var item = document.querySelector('.article[data-id="' + article.id + '"]');
          if (!item) return;
          if (article.archive_status === "pending") { stillWaiting = true; return; }

          item.dataset.archive = article.archive_status;
          var mark = item.querySelector('[data-role="archive-mark"]');
          if (mark) {
            var thin = article.archive_status === "ok" && article.archive_error;
            var state = thin ? "thin" : article.archive_status;
            var label = MARK_LABELS[state]
              + (MARK_VIA[article.archive_source] || "");
            /* The shape itself lives in CSS; only the state changes here. */
            mark.className = "mark mark--" + state;
            mark.dataset.mark = state;
            mark.dataset.source = article.archive_source || "direct";
            mark.setAttribute("aria-label", label);
            mark.title = label
              + (article.archive_error ? " \u2014 " + article.archive_error : "");
          }
          var title = item.querySelector(".title");
          if (title && article.title) title.textContent = article.title;
        });
        pollTimer = stillWaiting ? setTimeout(poll, 2500) : null;
      })
      .catch(function () { pollTimer = null; });
  }

  function startPolling() {
    if (pollTimer === null && pendingIds().length) {
      pollTimer = setTimeout(poll, 1500);
    }
  }

  startPolling();

  /* Coming back to the list after finishing a piece, the browser may show
     the copy it kept from before -- with that piece still unread. */
  window.addEventListener("pageshow", function (event) {
    if (!document.querySelector(".articles")) return;
    var nav = performance.getEntriesByType
      && performance.getEntriesByType("navigation")[0];
    if (event.persisted || (nav && nav.type === "back_forward")) {
      window.location.reload();
    }
  });

  /* Reaching the end of a piece marks it read. It has to be scrolled to,
     not just landed on: a short piece that fits on screen, or a reload that
     restores the scroll position, is not reading to the end. Marking it
     unread again by hand sticks for the rest of the session. */
  var reader = document.querySelector(".reader[data-id]");
  var readEnd = reader && reader.querySelector('[data-role="read-end"]');
  if (readEnd && "IntersectionObserver" in window) {
    var readerId = reader.dataset.id;
    var keepKey = "obomoboe:kept-unread:" + readerId;
    var statusForm = reader.querySelector('[data-role="status-form"]');

    if (statusForm) {
      statusForm.addEventListener("submit", function () {
        var target = statusForm.querySelector('[name="status"]').value;
        try {
          if (target === "unread") sessionStorage.setItem(keepKey, "1");
          else sessionStorage.removeItem(keepKey);
        } catch (e) { /* storage blocked; auto-read may just re-mark it */ }
      });
    }

    var keptUnread = false;
    try { keptUnread = sessionStorage.getItem(keepKey) === "1"; } catch (e) {}

    if (reader.dataset.status === "unread" && !keptUnread) {
      var armed = false;
      var observer = new IntersectionObserver(function (entries) {
        var visible = entries[entries.length - 1].isIntersecting;
        if (!visible) { armed = true; return; }
        if (!armed) return;
        observer.disconnect();
        post("/a/" + readerId + "/status", { status: "read" })
          .then(function (data) { if (data.status === "read") showRead(); });
      });
      observer.observe(readEnd);
    }

    function showRead() {
      reader.dataset.status = "read";
      if (statusForm) {
        statusForm.querySelector('[name="status"]').value = "unread";
        statusForm.querySelector("button").textContent = "Mark unread";
      }
      var logged = reader.querySelector('[data-role="logged"]');
      if (logged && !logged.querySelector(".logged__read")) {
        var stamp = document.createElement("span");
        stamp.className = "logged__read";
        stamp.textContent = "read just now";
        logged.appendChild(stamp);
      }
    }
  }

  /* Add without losing scroll position or the current filter. */
  var addForm = document.getElementById("add-form");
  if (addForm) {
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var urlField = addForm.querySelector('input[name="url"]');
      var tagField = addForm.querySelector('input[name="tags"]');
      if (!urlField.value.trim()) return;

      post("/add", { url: urlField.value, tags: tagField.value })
        .then(function (data) {
          if (data.duplicate) {
            alert("Already on the list.");
            return;
          }
          urlField.value = "";
          tagField.value = "";
          window.location.reload();
        })
        .catch(function () { addForm.submit(); });
    });
  }

  /* Theme settings: redraw the page in whatever is picked, before saving.
     The CSS mirrors theme.css() on the server. */
  var themeForm = document.getElementById("theme");
  var presetData = document.getElementById("theme-presets");
  if (themeForm && presetData) {
    var presets = JSON.parse(presetData.textContent);
    var themeStyle = document.getElementById("theme-css");
    var TOOTH = {
      light: "--tooth-blend: multiply; --tooth-opacity: .22;",
      dark: "--tooth-blend: screen; --tooth-opacity: .1;"
    };

    function block(selector, palette, mode) {
      var body = Object.keys(palette).map(function (name) {
        return "--" + name + ": " + palette[name] + "; ";
      }).join("");
      return selector + " { " + body + TOOTH[mode] + " }";
    }

    function preview() {
      var picked = themeForm.querySelector('[name="preset"]:checked');
      var preset = picked && presets[picked.value];
      if (preset) {
        themeStyle.textContent = [
          block(":root", preset.light, "light"),
          "@media (prefers-color-scheme: dark) { "
            + block(':root:not([data-theme="light"])', preset.dark, "dark") + " }",
          block(':root[data-theme="dark"]', preset.dark, "dark")
        ].join("\n");
      }

      var mode = themeForm.querySelector('[name="mode"]:checked');
      if (mode && mode.value !== "auto") {
        document.documentElement.dataset.theme = mode.value;
      } else {
        delete document.documentElement.dataset.theme;
      }
    }

    themeForm.addEventListener("change", preview);
  }
})();
