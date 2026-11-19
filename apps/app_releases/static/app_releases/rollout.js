/* Панель поэтапной раскатки: слайдер, быстрые проценты, подтверждения, график. */
(function () {
  "use strict";

  var TONE_ICONS = { primary: "fa-rocket", success: "fa-check-double", warning: "fa-pause", danger: "fa-undo-alt" };

  function init() {
    var panel = document.querySelector("[data-rr-panel]");
    if (!panel) return;
    var form = panel.querySelector("[data-rr-form]");
    var dialog = panel.querySelector("[data-rr-dialog]");
    var pending = null;

    // ---------- Подтверждение ----------
    function ask(opts) {
      pending = opts;
      var tone = opts.tone || "primary";
      if (!dialog || typeof dialog.showModal !== "function") {
        if (window.confirm(opts.title + "\n\n" + opts.text)) submit();
        return;
      }
      dialog.querySelector("[data-rr-dialog-title]").textContent = opts.title;
      dialog.querySelector("[data-rr-dialog-text]").textContent = opts.text;
      var icon = dialog.querySelector("[data-rr-dialog-icon]");
      icon.className = "rr-dialog__icon rr-dialog__icon--" + tone;
      icon.innerHTML = '<i class="fas ' + (opts.icon || TONE_ICONS[tone] || "fa-question") + '"></i>';
      var confirmBtn = dialog.querySelector("[data-rr-confirm]");
      confirmBtn.className = "rr-btn rr-btn--" + tone;
      confirmBtn.textContent = opts.confirmLabel || "Подтвердить";
      dialog.showModal();
      // Для опасных действий фокус — на «Отмена».
      (tone === "danger" ? dialog.querySelector("[data-rr-cancel]") : confirmBtn).focus();
    }

    function submit() {
      if (!pending) return;
      form.elements.action.value = pending.action;
      if (pending.percentage !== undefined) form.elements.percentage.value = pending.percentage;
      var btn = dialog && dialog.querySelector("[data-rr-confirm]");
      if (btn) { btn.disabled = true; btn.textContent = "Сохраняем…"; }
      form.submit();
    }

    if (dialog) {
      dialog.querySelector("[data-rr-cancel]").addEventListener("click", function () { dialog.close(); });
      dialog.querySelector("[data-rr-confirm]").addEventListener("click", submit);
      dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
    }

    panel.querySelectorAll("[data-rr-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        ask({
          action: btn.dataset.rrAction,
          title: btn.dataset.rrTitle,
          text: btn.dataset.rrText,
          tone: btn.dataset.rrTone,
          confirmLabel: btn.textContent.trim(),
        });
      });
    });

    // ---------- Слайдер ----------
    var slider = panel.querySelector("[data-rr-slider]");
    if (slider) {
      var range = slider.querySelector("[data-rr-range]");
      var output = slider.querySelector("[data-rr-output]");
      var apply = slider.querySelector("[data-rr-apply]");
      var applyLabel = apply && apply.querySelector("[data-rr-apply-label]");
      var lockedMin = parseInt(slider.dataset.lockedMin, 10) || 0;
      var initial = parseInt(range.value, 10) || 0;
      var isDraft = slider.dataset.status === "draft";
      var chips = slider.querySelectorAll("[data-rr-preset]");

      var render = function (value) {
        range.style.setProperty("--rr-value", value + "%");
        output.textContent = value + "%";
        chips.forEach(function (chip) {
          chip.classList.toggle("is-active", parseInt(chip.dataset.rrPreset, 10) === value);
        });
        if (!apply) return;
        if (isDraft) {
          applyLabel.textContent = "Начать с " + value + "%";
          apply.disabled = false;
        } else {
          apply.disabled = value <= initial;
          applyLabel.textContent = value > initial ? "Увеличить до " + value + "%" : "Применить";
        }
      };

      var clamp = function (value) { return Math.max(lockedMin, Math.min(100, value)); };

      range.addEventListener("input", function () {
        var value = parseInt(range.value, 10);
        if (value < lockedMin) { value = lockedMin; range.value = value; }
        render(value);
      });

      var confirmPercent = function (value) {
        var action = apply ? apply.dataset.rrApply : "set_percentage";
        var text = isDraft
          ? "Обновление начнут получать " + value + "% пользователей. Тестировщики получат его в любом случае."
          : "Обновление станет доступно " + value + "% пользователей (сейчас " + initial + "%). Уменьшить процент потом будет нельзя.";
        if (value === 100 && !isDraft) text = "Обновление станет доступно всем пользователям, релиз перейдёт в статус «Выпущен для всех».";
        ask({
          action: action,
          percentage: value,
          title: isDraft ? "Начать раскатку на " + value + "%?" : "Увеличить раскатку до " + value + "%?",
          text: text,
          tone: value === 100 ? "success" : "primary",
          icon: isDraft ? "fa-rocket" : "fa-arrow-up",
          confirmLabel: isDraft ? "Начать" : "Увеличить",
        });
      };

      chips.forEach(function (chip) {
        chip.addEventListener("click", function () {
          var value = clamp(parseInt(chip.dataset.rrPreset, 10));
          range.value = value;
          render(value);
          if (isDraft || value > initial) confirmPercent(value);
        });
      });

      if (apply) apply.addEventListener("click", function () { confirmPercent(parseInt(range.value, 10)); });
      render(initial);
    }

    // ---------- График ----------
    var chartHost = panel.querySelector("[data-rr-chart]");
    var dataNode = document.getElementById("rr-chart-data");
    if (chartHost && dataNode) drawChart(chartHost, JSON.parse(dataNode.textContent));
  }

  function el(name, attrs, text) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function drawChart(host, rows) {
    var width = 640, rowH = 34, labelW = 132, valueW = 92, top = 4;
    var height = rows.length * rowH + top;
    var max = Math.max.apply(null, rows.map(function (r) { return r.devices; })) || 1;
    var barMax = width - labelW - valueW;
    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, role: "img" });

    rows.forEach(function (row, i) {
      var y = top + i * rowH;
      var color = row.current ? "#2563eb" : row.newer ? "#7c3aed" : "#cbd5e1";
      var g = el("g");
      g.appendChild(el("title", {}, "Сборка " + row.build + (row.version ? " (" + row.version + ")" : "") + ": " + row.devices + " устр., " + row.share + "%"));
      g.appendChild(el("text", { x: 0, y: y + 15, "font-size": 13, "font-weight": row.current ? 700 : 600, fill: "#0f172a" }, row.version || "—"));
      g.appendChild(el("text", { x: 0, y: y + 29, "font-size": 11, fill: "#64748b" }, "сборка " + row.build));
      g.appendChild(el("rect", { x: labelW, y: y + 6, width: barMax, height: 18, rx: 6, fill: "#f1f5f9" }));
      var w = Math.max(4, Math.round((row.devices / max) * barMax));
      var bar = el("rect", { class: "rr-bar", x: labelW, y: y + 6, width: w, height: 18, rx: 6, fill: color });
      g.appendChild(bar);
      g.appendChild(el("text", { x: width, y: y + 20, "text-anchor": "end", "font-size": 13, "font-weight": 700, fill: "#0f172a" }, row.devices.toLocaleString("ru-RU")));
      g.appendChild(el("text", { x: width - 52, y: y + 20, "text-anchor": "end", "font-size": 11, fill: "#64748b" }, row.share + "%"));
      svg.appendChild(g);
    });
    host.appendChild(svg);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
