"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  // Local noon avoids UTC conversion and daylight-saving midnight boundaries.
  function dateValue(value) {
    return new Date(`${value}T12:00:00`);
  }
  function dateISO(value) {
    return `${String(value.getFullYear()).padStart(4, "0")}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }
  function dateText(value) {
    return dateValue(value).toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
      weekday: "short",
    });
  }
  function updateDueLabel() {
    const value = $("#taskDue").value;
    $("#dueLabel").textContent = value ? dateText(value) : "设置截止日期";
    $("#dueButton").classList.toggle("has-date", Boolean(value));
  }
  window.updateDueLabel = updateDueLabel;
  let dateFocus;
  function renderDatePicker(focus = true) {
    const selected = $("#taskDue").value;
    const today = dateISO(new Date());
    const first = new Date(dateFocus);
    first.setDate(1);
    $("#dueMonth").textContent =
      `${first.getFullYear()} 年 ${first.getMonth() + 1} 月`;
    $("#datePrevious").disabled =
      first.getFullYear() === 1 && first.getMonth() === 0;
    $("#dateNext").disabled =
      first.getFullYear() === 9999 && first.getMonth() === 11;
    const cursor = new Date(first);
    cursor.setDate(1 - ((first.getDay() + 6) % 7));
    let html = "";
    for (let row = 0; row < 6; row++) {
      html += '<div class="date-row" role="row">';
      for (let col = 0; col < 7; col++) {
        const iso = dateISO(cursor),
          isSelected = iso === selected;
        const outside = cursor.getMonth() !== first.getMonth();
        const invalid = cursor.getFullYear() < 1 || cursor.getFullYear() > 9999;
        html += `<div role="gridcell" aria-selected="${isSelected}"><button type="button" data-date="${iso}" tabindex="${iso === dateISO(dateFocus) ? 0 : -1}" class="date-day ${outside ? "outside" : ""} ${iso === today ? "today" : ""} ${isSelected ? "selected" : ""}" aria-label="${invalid ? "超出日期范围" : dateText(iso)}" ${iso === today ? 'aria-current="date"' : ""} ${invalid ? "disabled" : ""}>${cursor.getDate()}</button></div>`;
        cursor.setDate(cursor.getDate() + 1);
      }
      html += "</div>";
    }
    $("#dateGrid").innerHTML = html;
    if (focus) $("#dateGrid [tabindex='0']").focus();
  }
  function selectDue(value) {
    $("#taskDue").value = value;
    $("#taskDue").dispatchEvent(new Event("input", { bubbles: true }));
    updateDueLabel();
    $("#dateDialog").close();
  }
  function shiftDateMonth(delta) {
    const next = new Date(dateFocus),
      day = next.getDate();
    next.setDate(1);
    next.setMonth(next.getMonth() + delta);
    if (next.getFullYear() < 1 || next.getFullYear() > 9999) return;
    const end = new Date(next);
    end.setMonth(end.getMonth() + 1);
    end.setDate(0);
    next.setDate(Math.min(day, end.getDate()));
    dateFocus = next;
    renderDatePicker();
  }
  $("#dueButton").onclick = () => {
    const value = $("#taskDue").value;
    dateFocus = dateValue(value || dateISO(new Date()));
    $("#dateSummary").textContent = value
      ? `当前截止：${dateText(value)}`
      : "选择一个日期，为下一步留出时间。";
    renderDatePicker(false);
    $("#dateDialog").showModal();
    $("#dateGrid [tabindex='0']").focus();
  };
  $("#dateClose").onclick = () => $("#dateDialog").close();
  $("#dateDialog").addEventListener("close", () => $("#dueButton").focus());
  $("#datePrevious").onclick = () => shiftDateMonth(-1);
  $("#dateNext").onclick = () => shiftDateMonth(1);
  $("#dateClear").onclick = () => selectDue("");
  $$("[data-offset]").forEach(
    (button) =>
      (button.onclick = () => {
        const next = dateValue(dateISO(new Date()));
        next.setDate(next.getDate() + Number(button.dataset.offset));
        selectDue(dateISO(next));
      }),
  );
  $("#dateGrid").onclick = (event) => {
    const button = event.target.closest("[data-date]");
    if (button && !button.disabled) selectDue(button.dataset.date);
  };
  $("#dateGrid").onkeydown = (event) => {
    if (event.isComposing) return;
    const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (event.key === "PageUp" || event.key === "PageDown") {
      event.preventDefault();
      shiftDateMonth(
        (event.key === "PageUp" ? -1 : 1) * (event.shiftKey ? 12 : 1),
      );
      return;
    }
    let offset = offsets[event.key];
    if (event.key === "Home") offset = -(dateFocus.getDay() + 6) % 7;
    if (event.key === "End") offset = 6 - ((dateFocus.getDay() + 6) % 7);
    if (offset === undefined) return;
    event.preventDefault();
    const next = new Date(dateFocus);
    next.setDate(next.getDate() + offset);
    if (next.getFullYear() < 1 || next.getFullYear() > 9999) return;
    dateFocus = next;
    renderDatePicker();
  };
})();
