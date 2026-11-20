(() => {
  'use strict';
  const url = document.currentScript.dataset.url;
  let pending = false;
  let expired = false;

  async function sendPresence() {
    if (pending || expired) return;
    pending = true;
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin', cache: 'no-store'});
      if (response.status === 401) {
        expired = true;
        clearInterval(timer);
      }
    } catch { /* Try again on the next check-in. */ }
    finally { pending = false; }
  }

  const timer = setInterval(sendPresence, 45000);
  window.addEventListener('pageshow', event => { if (event.persisted) sendPresence(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) sendPresence(); });
  void sendPresence();
})();
