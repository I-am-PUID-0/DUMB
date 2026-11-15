/* Native, keyboard-accessible image enlargement without a CDN dependency. */
(() => {
  let dialog;
  function initializeImages() {
    document.querySelectorAll('article img').forEach(img => {
      if (img.closest('a, button, .docs-home') || img.classList.contains('twemoji')) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'docs-image-button';
      button.setAttribute('aria-label', `Enlarge image: ${img.alt || 'documentation screenshot'}`);
      img.replaceWith(button);
      button.append(img);
      button.addEventListener('click', () => {
        if (!dialog) {
          dialog = document.createElement('dialog');
          dialog.className = 'docs-lightbox';
          dialog.setAttribute('aria-label', 'Enlarged documentation image');
          const close = document.createElement('button');
          close.type = 'button';
          close.textContent = 'Close image';
          close.addEventListener('click', () => dialog.close());
          dialog.append(document.createElement('img'), close);
          document.body.append(dialog);
        }
        const enlarged = dialog.querySelector('img');
        enlarged.src = img.currentSrc || img.src;
        enlarged.alt = img.alt;
        dialog.showModal();
      });
    });
  }
  if (typeof document$ !== 'undefined') document$.subscribe(initializeImages);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeImages);
  else initializeImages();
})();
