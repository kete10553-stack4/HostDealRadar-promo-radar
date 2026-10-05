'use strict';
// Deep links open the exact evidence record, including on direct page loads.
function revealRecord() {
  let id;
  try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { return; }
  const record = id && document.getElementById(id);
  if (record && record.matches('details.plan-evidence')) {
    record.open = true;
    record.scrollIntoView({ block: 'start' });
  }
}
window.addEventListener('hashchange', revealRecord);
revealRecord();
for (const button of document.querySelectorAll('[data-copy-record]')) {
  if (!navigator.clipboard || !window.isSecureContext) continue;
  button.hidden = false;
  button.addEventListener('click', async () => {
    const status = button.parentElement.querySelector('.copy-status');
    try {
      await navigator.clipboard.writeText(new URL(button.dataset.copyRecord, window.location.origin).href);
      status.textContent = 'Link copied.';
    } catch {
      status.textContent = 'Use the record link to copy its address.';
    }
  });
}
// An old static snapshot must not keep looking like a current offer.
for (const node of document.querySelectorAll('[data-fresh-until]')) {
  const refresh = () => {
    if (Date.now() > Date.parse(node.dataset.freshUntil)) node.classList.add('stale');
  };
  refresh();
  setInterval(refresh, 60000);
}
