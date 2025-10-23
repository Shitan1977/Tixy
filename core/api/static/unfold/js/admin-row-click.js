document.addEventListener("DOMContentLoaded", function () {
  var rows = document.querySelectorAll(
    ".change-list table.results tbody tr, .unfold-changelist tbody tr, table.result-list tbody tr"
  );
  rows.forEach(function (tr) {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input, select, label")) return;
      var link = tr.querySelector("th a, td a");
      if (link && link.href) {
        window.location.href = link.href;
        return;
      }
      var dl = tr.getAttribute("data-link");
      if (dl) window.location.href = dl;
    });
  });
});