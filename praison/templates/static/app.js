// All client behaviour lives here (no inline scripts/handlers) so the page can
// ship a strict Content-Security-Policy: script-src 'self'.

// Apply the saved theme before first paint to avoid a flash. This file is
// loaded synchronously in <head>, so it runs before the body is rendered.
document.documentElement.dataset.theme =
  localStorage.getItem("praison-theme") || "dark";

// htmx injects an inline <style> for its indicator by default; we don't use
// indicators, so disable it to keep style-src tight.
document.addEventListener("htmx:config", function () {
  if (window.htmx) window.htmx.config.includeIndicatorStyles = false;
});

function closeModal() {
  var modal = document.getElementById("modal");
  if (modal) modal.innerHTML = "";
}

// Delegated click handling: works for HTMX-swapped fragments (modals) too,
// since the listener lives on a node that is never replaced.
document.addEventListener("click", function (event) {
  var trigger = event.target.closest("[data-action]");
  if (trigger) {
    var action = trigger.dataset.action;
    if (action === "toggle-theme") {
      var next =
        document.documentElement.dataset.theme === "dark" ? "sepia" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("praison-theme", next);
      return;
    }
    if (action === "close-modal") {
      closeModal();
      return;
    }
  }
  // Click on the modal backdrop (outside the dialog) closes the modal.
  if (event.target.classList && event.target.classList.contains("modal-backdrop")) {
    closeModal();
  }
});

// Settings/plan/delete forms target #content; on a successful swap into
// #content the modal has done its job, so close it. A validation error
// retargets to #modal (HX-Retarget), so the dialog stays open in that case.
document.addEventListener("htmx:afterSwap", function (event) {
  if (event.detail.target && event.detail.target.id === "content") {
    closeModal();
  }
});
