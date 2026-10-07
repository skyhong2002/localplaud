'use strict';
// Authentication pages end the previous workspace's client-side history.
if (document.body.classList.contains('auth-shell')) {
  try { localStorage.removeItem('htmx-history-cache'); sessionStorage.clear(); } catch (_) {}
}
document.addEventListener('submit', event => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (!form.closest('.account-page, .auth-shell')) return;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { event.preventDefault(); return; }
  const password = form.elements.namedItem('password');
  const confirmation = form.elements.namedItem('password_confirm');
  if (password && confirmation && password.value !== confirmation.value) {
    event.preventDefault(); confirmation.setCustomValidity('兩次輸入的密碼不相同。'); confirmation.reportValidity();
    confirmation.addEventListener('input', () => confirmation.setCustomValidity(''), {once:true});
    return;
  }
  if (form.dataset.submitLabel) {
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.dataset.originalLabel = button.textContent; button.textContent = form.dataset.submitLabel; button.disabled = true; }
    form.setAttribute('aria-busy', 'true');
  }
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('button[data-original-label]').forEach(button => { button.textContent = button.dataset.originalLabel; button.disabled = false; });
  document.querySelectorAll('form[aria-busy]').forEach(form => form.removeAttribute('aria-busy'));
});
