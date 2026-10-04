'use strict';
(() => {
  const measurementId = document.currentScript?.dataset.ga4Id;
  if (!/^G-[A-Z0-9]+$/.test(measurementId || '')) return;
  const preferenceKey = 'hostdealradar-analytics-consent-v1';
  const lifetime = 180 * 24 * 60 * 60 * 1000;
  let choice = null;
  try {
    const saved = JSON.parse(localStorage.getItem(preferenceKey) || 'null');
    if (saved && ['granted', 'denied'].includes(saved.choice) && Number.isFinite(saved.savedAt)
        && saved.savedAt <= Date.now() && Date.now() - saved.savedAt < lifetime) choice = saved.choice;
  } catch (_) { /* Without preference storage, ask again rather than assume consent. */ }

  window.dataLayer = window.dataLayer || [];
  window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
  window.gtag('consent', 'default', {
    analytics_storage: 'denied', ad_storage: 'denied',
    ad_user_data: 'denied', ad_personalization: 'denied'
  });
  let started = false;
  function startAnalytics() {
    window['ga-disable-' + measurementId] = false;
    window.gtag('consent', 'update', {analytics_storage: 'granted'});
    if (started) return;
    started = true;
    const cleanUrl = value => {
      try { const url = new URL(value); return url.origin + url.pathname; }
      catch (_) { return ''; }
    };
    window.gtag('js', new Date());
    window.gtag('config', measurementId, {
      allow_google_signals: false, allow_ad_personalization_signals: false,
      page_location: cleanUrl(location.href), page_referrer: cleanUrl(document.referrer),
      cookie_expires: 180 * 24 * 60 * 60
    });
    const tag = document.createElement('script');
    tag.async = true;
    tag.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(measurementId);
    document.head.appendChild(tag);
  }
  function stopAnalytics() {
    window['ga-disable-' + measurementId] = true;
    if (started) window.gtag('consent', 'update', {analytics_storage: 'denied'});
    const domains = ['', location.hostname, '.' + location.hostname];
    for (const cookie of document.cookie.split(';')) {
      const name = cookie.split('=')[0].trim();
      if (name !== '_ga' && !name.startsWith('_ga_')) continue;
      for (const domain of domains) {
        document.cookie = name + '=; Max-Age=0; path=/; SameSite=Lax' + (domain ? '; domain=' + domain : '');
      }
    }
  }
  const banner = document.createElement('aside');
  banner.className = 'analytics-consent';
  banner.setAttribute('aria-label', 'Analytics cookie choice');
  banner.innerHTML = '<div><strong>Analytics cookies</strong><p>Allow Google Analytics to measure visits and help improve HostDealRadar. You can change your choice in the footer. <a href="/privacy/">Privacy details</a></p></div><div class="analytics-actions"><button type="button" data-choice="granted">Allow analytics</button><button type="button" data-choice="denied">Reject analytics</button></div>';
  function choose(value) {
    choice = value;
    try { localStorage.setItem(preferenceKey, JSON.stringify({choice, savedAt: Date.now()})); }
    catch (_) { /* This page still honours the selected choice. */ }
    if (choice === 'granted') startAnalytics(); else stopAnalytics();
    banner.hidden = true;
  }
  for (const button of banner.querySelectorAll('[data-choice]')) {
    button.addEventListener('click', () => choose(button.dataset.choice));
  }
  document.body.appendChild(banner);
  banner.hidden = choice !== null;
  document.querySelector('[data-analytics-settings]')?.addEventListener('click', () => {
    banner.hidden = false;
    banner.querySelector('button').focus();
  });
  if (choice === 'granted') startAnalytics();
})();
