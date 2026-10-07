/* Бюджетен монитор Несебър — client-side sort/search/filter for data tables.
 * Vanilla JS, no build step, no dependencies. Progressive enhancement: the
 * table works (and is fully readable/printable) with JS disabled; this only
 * adds sorting/filtering convenience on top of the plain HTML table. */
(function () {
  "use strict";

  function parseSortValue(cell) {
    var raw = cell.getAttribute("data-sort-value");
    if (raw === null) {
      raw = cell.textContent.trim();
    }
    var num = Number(raw);
    if (raw !== "" && !Number.isNaN(num)) {
      return num;
    }
    return raw.toLowerCase();
  }

  function enableSorting(table) {
    var thead = table.tHead;
    if (!thead) return;
    var headerRow = thead.rows[0];
    var tbody = table.tBodies[0];
    if (!tbody) return;

    Array.prototype.forEach.call(headerRow.cells, function (th, index) {
      if (!th.hasAttribute("data-sort")) return;
      th.setAttribute("tabindex", "0");
      th.setAttribute("role", "button");
      th.setAttribute("aria-label", "Подреди по тази колона");

      function toggleSort() {
        var current = th.getAttribute("data-sort");
        var direction = current === "asc" ? "desc" : "asc";

        Array.prototype.forEach.call(headerRow.cells, function (otherTh) {
          if (otherTh !== th && otherTh.hasAttribute("data-sort")) {
            otherTh.setAttribute("data-sort", "none");
          }
        });
        th.setAttribute("data-sort", direction);

        var rows = Array.prototype.slice.call(tbody.rows);
        rows.sort(function (rowA, rowB) {
          var a = parseSortValue(rowA.cells[index]);
          var b = parseSortValue(rowB.cells[index]);
          if (a < b) return direction === "asc" ? -1 : 1;
          if (a > b) return direction === "asc" ? 1 : -1;
          return 0;
        });
        rows.forEach(function (row) {
          tbody.appendChild(row);
        });
      }

      th.addEventListener("click", toggleSort);
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleSort();
        }
      });
    });
  }

  function enableFiltering(table) {
    var wrap = table.closest("[data-table-container]");
    if (!wrap) return;
    var searchInput = wrap.querySelector("[data-table-search]");
    var selects = Array.prototype.slice.call(wrap.querySelectorAll("[data-table-filter]"));
    var tbody = table.tBodies[0];
    var countEl = wrap.querySelector("[data-table-count]");
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);

    function applyFilters() {
      var query = searchInput ? searchInput.value.trim().toLowerCase() : "";
      var visible = 0;
      rows.forEach(function (row) {
        var matchesSearch = !query || row.textContent.toLowerCase().indexOf(query) !== -1;
        var matchesFilters = selects.every(function (select) {
          if (!select.value) return true;
          return row.getAttribute("data-" + select.getAttribute("data-table-filter")) === select.value;
        });
        var show = matchesSearch && matchesFilters;
        row.hidden = !show;
        if (show) visible += 1;
      });
      if (countEl) {
        countEl.textContent = visible.toLocaleString("en-US");
      }
    }

    if (searchInput) {
      searchInput.addEventListener("input", applyFilters);
    }
    selects.forEach(function (select) {
      select.addEventListener("change", applyFilters);
    });
    applyFilters();
  }

  // flags/index.html's flag list: a <details>/<summary> row per flag (so
  // "expand for the full explanation" needs no JS at all) with a search box,
  // a tier segmented control, and severity/rule <select>s filtering which
  // rows are hidden. Structurally similar to enableFiltering() above, but
  // over [data-flag-row] elements instead of <table> rows.
  function enableFlagFilters(container) {
    var rows = Array.prototype.slice.call(container.querySelectorAll("[data-flag-row]"));
    var searchInput = container.querySelector("[data-table-search]");
    var selects = Array.prototype.slice.call(container.querySelectorAll("[data-table-filter]"));
    var tierButtons = Array.prototype.slice.call(container.querySelectorAll("[data-tier-filter]"));
    var countEl = container.querySelector("[data-table-count]");
    var activeTier = "";
    if (!rows.length) return;

    function applyFilters() {
      var query = searchInput ? searchInput.value.trim().toLowerCase() : "";
      var visible = 0;
      rows.forEach(function (row) {
        var matchesSearch = !query || row.textContent.toLowerCase().indexOf(query) !== -1;
        var matchesSelects = selects.every(function (select) {
          if (!select.value) return true;
          return row.getAttribute("data-" + select.getAttribute("data-table-filter")) === select.value;
        });
        var matchesTier = !activeTier || row.getAttribute("data-tier") === activeTier;
        var show = matchesSearch && matchesSelects && matchesTier;
        row.hidden = !show;
        if (show) visible += 1;
      });
      if (countEl) countEl.textContent = visible.toLocaleString("en-US");
    }

    if (searchInput) searchInput.addEventListener("input", applyFilters);
    selects.forEach(function (select) {
      select.addEventListener("change", applyFilters);
    });
    tierButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        activeTier = btn.getAttribute("data-tier-filter") || "";
        tierButtons.forEach(function (b) {
          b.setAttribute("aria-pressed", String(b === btn));
        });
        applyFilters();
      });
    });
    applyFilters();
  }

  // Generic "copy this text" button: copies the nearest [data-copy-source]'s
  // text (inside the same <details>) to the clipboard, with a textarea
  // fallback for browsers/contexts without the async Clipboard API.
  function enableCopyButtons() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-copy-button]"), function (btn) {
      btn.addEventListener("click", function () {
        var host = btn.closest("details") || btn.parentElement;
        var source = host ? host.querySelector("[data-copy-source]") : null;
        if (!source) return;
        var text = source.textContent;
        var original = btn.textContent;
        function showCopied() {
          btn.textContent = "Копирано ✓";
          setTimeout(function () {
            btn.textContent = original;
          }, 1800);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(showCopied, showCopied);
          return;
        }
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try {
          document.execCommand("copy");
        } catch (e) {
          /* no-op: clipboard just won't be populated */
        }
        document.body.removeChild(ta);
        showCopied();
      });
    });
  }

  // "/" focuses the page's search field (as in most keyboard-first apps),
  // unless the user is already typing in a form control.
  function enableSearchShortcut() {
    var search = document.querySelector("[data-table-search]");
    if (!search) return;
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      event.preventDefault();
      search.focus();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("table[data-sortable]"), enableSorting);
    Array.prototype.forEach.call(document.querySelectorAll("table[data-filterable]"), enableFiltering);
    Array.prototype.forEach.call(document.querySelectorAll("[data-flag-filters]"), enableFlagFilters);
    enableCopyButtons();
    enableSearchShortcut();
  });
})();
