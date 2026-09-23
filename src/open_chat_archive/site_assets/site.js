/* Progressive enhancement only: no network, no content parsing. */
(() => {
  const input = document.querySelector('[data-catalog-filter]');
  if (!input) return;
  input.addEventListener('input', () => {
    const needle = input.value.toLowerCase();
    document.querySelectorAll('[data-catalog-row]').forEach((row) => {
      row.classList.toggle('hidden', !row.textContent.toLowerCase().includes(needle));
    });
  });
})();
