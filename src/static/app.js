/* Progressive enhancement only -- every action here also works as a plain
   form POST if JS is off. */
(function () {
  "use strict";

  var PILL_LABELS = {
    pending: "archiving...",
    ok: "archived",
    failed: "archive failed"
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

    if (trigger.dataset.action === "toggle") {
      post("/a/" + id + "/status", {}).then(function (data) {
        item.classList.toggle("is-read", data.status === "read");
        var label = "Mark " + (data.status === "read" ? "unread" : "read");
        trigger.title = label;
        trigger.setAttribute("aria-label", label);
      });
    }

    if (trigger.dataset.action === "delete") {
      if (!confirm("Delete this article and its archive?")) return;
      post("/a/" + id + "/delete", {}).then(function () { item.remove(); });
    }

    if (trigger.dataset.action === "add-tag") {
      showTagInput(item, trigger, id);
    }

    if (trigger.dataset.action === "accept-suggestion") {
      var name = trigger.dataset.tag;
      trigger.disabled = true;
      post("/a/" + id + "/suggestions/accept", { tag: name })
        .then(function (data) {
          trigger.remove();
          renderTags(item, data.tags);
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
    var container = item.querySelector('[data-role="tags"]');
    var add = container.querySelector(".chip--add");
    container.querySelectorAll("a.chip").forEach(function (chip) {
      chip.remove();
    });
    var firstSuggestion = container.querySelector(".chip--suggest");
    tags.forEach(function (name) {
      var chip = document.createElement("a");
      chip.className = "chip chip--sm";
      chip.href = "/?tag=" + encodeURIComponent(name);
      chip.textContent = name;
      container.insertBefore(chip, firstSuggestion || add);
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
          var pill = item.querySelector('[data-role="archive-pill"]');
          if (pill) {
            var thin = article.archive_status === "ok" && article.archive_error;
            pill.className = "pill pill--" + (thin ? "thin" : article.archive_status);
            pill.textContent = (thin ? "partial" : PILL_LABELS[article.archive_status])
              + (article.archive_source === "archive.today" ? " via archive.today" : "");
            pill.title = article.archive_error || "";
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
})();
