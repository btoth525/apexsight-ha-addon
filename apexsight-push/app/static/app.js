// Tiny progressive-enhancement helpers kept in an external file so the admin
// pages need no inline scripts (lets the relay ship a strict CSP).

// Confirm before submitting any form that carries a data-confirm message.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) {
    e.preventDefault();
  }
});

// Copy-to-clipboard for any element with data-copy (e.g. the API token).
document.addEventListener("click", function (e) {
  var el = e.target.closest ? e.target.closest("[data-copy]") : null;
  if (!el) return;
  var text = el.getAttribute("data-copy");
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text);
  }
  var original = el.textContent;
  el.textContent = "Copied ✓";
  setTimeout(function () { el.textContent = original; }, 1200);
});
