'use strict';
// An old static snapshot must not keep looking like a current offer.
for (const node of document.querySelectorAll('[data-fresh-until]')) {
  const refresh = () => {
    if (Date.now() > Date.parse(node.dataset.freshUntil)) node.classList.add('stale');
  };
  refresh();
  setInterval(refresh, 60000);
}
