// Which month the agenda is showing.
//
// Every month is already on the page — the layout builds them all — so this
// only decides which one is visible and moves between them. Three consequences
// worth keeping: a reader with no JavaScript sees every month stacked instead of
// a blank page, moving months makes no request, and the month it starts on is
// decided here rather than when the site was built, so it is right however long
// ago that was.
(function () {
  "use strict";

  var months = [].slice.call(document.querySelectorAll(".agenda__month"));
  if (months.length === 0) return;

  var label = document.querySelector("[data-agenda-label]");
  var buttons = [].slice.call(document.querySelectorAll("[data-agenda-move]"));
  var keys = months.map(function (section) { return section.dataset.month; });

  // The month the reader is in, or the nearest one the page has.
  var now = new Date();
  var current = now.getFullYear() + "-" + ("0" + (now.getMonth() + 1)).slice(-2);
  var index = keys.indexOf(current);
  if (index === -1) {
    index = 0;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i] <= current) index = i;
    }
  }

  function show(next) {
    index = Math.max(0, Math.min(months.length - 1, next));
    months.forEach(function (section, i) {
      section.hidden = i !== index;
    });
    if (label) label.textContent = months[index].dataset.label;
    buttons.forEach(function (button) {
      var step = parseInt(button.dataset.agendaMove, 10);
      button.disabled = index + step < 0 || index + step > months.length - 1;
    });
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      show(index + parseInt(button.dataset.agendaMove, 10));
    });
  });

  // Only now: until this runs the page is the no-JavaScript version, every
  // month visible, which is what a crawler and a reader without scripts get.
  document.documentElement.classList.add("has-agenda-script");
  show(index);
})();
