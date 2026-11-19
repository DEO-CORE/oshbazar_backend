/* Фича-флаги: переключатели, быстрые проценты, слайдер — всё через подтверждение. */
(function () {
  "use strict";

  function init() {
    var form = document.querySelector("[data-ff-form]");
    var dialog = document.querySelector("[data-ff-dialog]");
    if (!form) return;
    var pending = null;

    function submit() {
      if (!pending) return;
      form.action = pending.url;
      form.elements.action.value = pending.action;
      form.elements.percentage.value = pending.percentage == null ? "" : pending.percentage;
      form.elements.next.value = window.location.pathname + window.location.search;
      var btn = dialog && dialog.querySelector("[data-ff-confirm]");
      if (btn) { btn.disabled = true; btn.textContent = "Сохраняем…"; }
      form.submit();
    }

    function ask(opts) {
      pending = opts;
      if (!dialog || typeof dialog.showModal !== "function") {
        if (window.confirm(opts.title + "\n\n" + opts.text)) submit();
        return;
      }
      var tone = opts.tone || "primary";
      dialog.querySelector("[data-ff-dialog-title]").textContent = opts.title;
      dialog.querySelector("[data-ff-dialog-text]").textContent = opts.text;
      var icon = dialog.querySelector("[data-ff-dialog-icon]");
      icon.className = "rr-dialog__icon rr-dialog__icon--" + tone;
      icon.innerHTML = '<i class="fas ' + (opts.icon || "fa-question") + '"></i>';
      var ok = dialog.querySelector("[data-ff-confirm]");
      ok.className = "rr-btn rr-btn--" + tone;
      ok.disabled = false;
      ok.textContent = opts.confirm || "Подтвердить";
      dialog.showModal();
      (tone === "danger" ? dialog.querySelector("[data-ff-cancel]") : ok).focus();
    }

    if (dialog) {
      dialog.querySelector("[data-ff-cancel]").addEventListener("click", function () { dialog.close(); });
      dialog.querySelector("[data-ff-confirm]").addEventListener("click", submit);
      dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
    }

    function fromButton(btn) {
      return {
        url: btn.dataset.ffUrl,
        action: btn.dataset.ffAction,
        percentage: btn.dataset.ffPercentage,
        tone: btn.dataset.ffTone,
        icon: btn.dataset.ffIcon,
        title: btn.dataset.ffTitle,
        text: btn.dataset.ffText,
        confirm: btn.dataset.ffConfirm,
      };
    }

    // Делегирование: кнопки есть и в списке (внутри формы действий), и в карточке флага.
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-ff-submit]");
      if (!btn || btn.disabled) return;
      e.preventDefault();
      ask(fromButton(btn));
    });

    // Слайдер на странице флага.
    var slider = document.querySelector("[data-ff-slider]");
    if (!slider) return;
    var range = slider.querySelector("[data-ff-range]");
    var output = slider.querySelector("[data-ff-output]");
    var apply = slider.querySelector("[data-ff-apply]");
    var label = apply && apply.querySelector("[data-ff-apply-label]");
    var initial = parseInt(slider.dataset.ffInitial, 10) || 0;
    var enabled = slider.dataset.ffEnabled === "1";

    range.addEventListener("input", function () {
      var v = parseInt(range.value, 10);
      range.style.setProperty("--rr-value", v + "%");
      output.textContent = v + "%";
      slider.querySelectorAll(".rr-chip").forEach(function (chip) {
        chip.classList.toggle("is-active", parseInt(chip.dataset.ffPercentage, 10) === v);
      });
      if (!apply) return;
      apply.disabled = v === initial;
      label.textContent = v === initial ? "Применить" : (v > initial ? "Увеличить" : "Уменьшить") + " до " + v + "%";
    });

    if (apply) {
      apply.addEventListener("click", function () {
        var v = parseInt(range.value, 10);
        var down = v < initial;
        ask({
          url: slider.dataset.ffUrl,
          action: "set_percentage",
          percentage: v,
          tone: down ? "warning" : "primary",
          icon: down ? "fa-arrow-down" : "fa-arrow-up",
          title: (down ? "Уменьшить" : "Увеличить") + " до " + v + "%?",
          text: down
            ? "Флаг пропадёт у части пользователей: останутся только попавшие в " + v + "%."
            : "Флаг получат " + v + "% пользователей. Все, кто уже его получил, его сохранят." +
              (enabled ? "" : " Флаг выключен — процент вступит в силу после включения."),
          confirm: down ? "Уменьшить" : "Увеличить",
        });
      });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
