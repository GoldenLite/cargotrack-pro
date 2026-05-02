/* SLA cell utility: renders progress bar HTML from state dict
   and ticks all cells on a shared 1s interval. */
(function () {
  'use strict';

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'
    }[c]));
  }

  function formatRemaining(seconds) {
    const abs = Math.abs(seconds);
    const h = Math.floor(abs / 3600);
    const m = Math.floor((abs % 3600) / 60);
    const s = abs % 60;
    const sign = seconds < 0 ? '-' : '';
    if (h > 0) return `${sign}${h}ч ${m}м`;
    if (m > 0) return `${sign}${m}м ${s}с`;
    return `${sign}${s}с`;
  }

  function computeColor(remaining, total, warnPct) {
    if (remaining <= 0) return '#dc2626';
    const ratio = Math.min(1, remaining / total);
    const warnRatio = 1 - (warnPct / 100);
    if (ratio > warnRatio) {
      const t = (ratio - warnRatio) / (1 - warnRatio);
      const hue = 60 + 60 * t;
      return `hsl(${hue}, 70%, 45%)`;
    } else {
      const t = ratio / warnRatio;
      const hue = 60 * t;
      return `hsl(${hue}, 75%, 48%)`;
    }
  }

  window.renderSlaCell = function (state) {
    if (!state) return '<span class="sla-dash">—</span>';
    const { deadline_iso, total_seconds, warning_threshold_pct } = state;
    return `<div class="sla-cell" data-deadline="${escHtml(deadline_iso)}" `
         + `data-total="${total_seconds}" data-warn="${warning_threshold_pct}">`
         + `<div class="sla-bar"><div class="sla-fill"></div></div>`
         + `<div class="sla-text"></div></div>`;
  };

  function tickCell(el) {
    const deadline = Date.parse(el.dataset.deadline);
    const total    = parseInt(el.dataset.total, 10);
    const warnPct  = parseInt(el.dataset.warn, 10) || 75;
    if (!deadline || !total) return;
    const remaining = Math.floor((deadline - Date.now()) / 1000);
    const ratio = Math.max(0, Math.min(1, remaining / total));
    const fill  = el.querySelector('.sla-fill');
    const text  = el.querySelector('.sla-text');
    const breached = remaining < 0;
    fill.style.width = (ratio * 100) + '%';
    fill.style.background = computeColor(remaining, total, warnPct);
    text.textContent = breached ? 'Просрочено ' + formatRemaining(remaining) : formatRemaining(remaining);
    el.classList.toggle('sla-breached', breached);
  }

  function tickAll() {
    document.querySelectorAll('.sla-cell').forEach(tickCell);
  }

  // Kick off a single shared ticker.
  if (!window.__slaTickerStarted) {
    window.__slaTickerStarted = true;
    document.addEventListener('DOMContentLoaded', tickAll);
    setInterval(tickAll, 1000);
  }
  window.tickSlaCells = tickAll;
})();
