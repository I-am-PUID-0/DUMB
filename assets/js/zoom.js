document$.subscribe(() => {
  mediumZoom('article img:not(a img)', {
    background: 'rgba(0, 0, 0, 0.85)',
    margin: 24,
    scrollOffset: 40
  });
});
