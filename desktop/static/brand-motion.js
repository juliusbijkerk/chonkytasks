/* Small, randomly paced hops and full turns for the chonkytasks mascot. */
(() => {
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const motions = new Map();
  const random = (min, max) => min + Math.random() * (max - min);

  function cycle(state) {
    state.animation?.cancel();
    const spin = state.element.dataset.chonkMotion === 'spin';
    const side = (Math.random() < .5 ? -1 : 1) * random(3, 6);
    const height = random(8, 13);
    const keyframes = spin ? [
      {transform: 'rotate(0deg)'}, {transform: 'rotate(360deg)'}
    ] : [
      {offset: 0, transform: 'translate(0, 0) rotate(0deg) scale(1)'},
      {offset: .08, transform: 'translate(0, 1px) scale(1.12, .88)'},
      {offset: .25, transform: `translate(${side}px, -${height}px) rotate(${side * 1.4}deg) scale(.96, 1.04)`},
      {offset: .42, transform: `translate(${side}px, 1px) scale(1.1, .9)`},
      {offset: .49, transform: `translate(${side}px, 0) scale(1)`},
      {offset: .64, transform: `translate(${-side}px, -${height * .7}px) rotate(${-side}deg)`},
      {offset: .79, transform: 'translate(0, 1px) scale(1.1, .9)'},
      {offset: .86, transform: 'translate(0, 0) rotate(0deg) scale(1)'},
      {offset: 1, transform: 'translate(0, 0) rotate(0deg) scale(1)'}
    ];
    state.animation = state.element.animate(keyframes, {
      duration: spin ? random(850, 2200) : random(1800, 3000),
      easing: spin ? 'cubic-bezier(.4, 0, .6, 1)' : 'linear'
    });
    state.animation.onfinish = () => reconcile(state);
  }

  function reconcile(state) {
    if (reducedMotion.matches || !state.element.isConnected || (state.element.dataset.chonkMotion === 'hop' && !state.hovered)) {
      state.animation?.cancel();
      state.animation = null;
    } else if (document.hidden || !state.visible) {
      if (state.animation?.playState === 'running') state.animation.pause();
    } else if (!state.animation || state.animation.playState === 'finished') {
      cycle(state);
    } else if (state.animation.playState === 'paused') {
      state.animation.play();
    }
  }

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      const state = motions.get(entry.target);
      if (state) { state.visible = entry.isIntersecting; reconcile(state); }
    });
  });

  function refresh() {
    motions.forEach((state, element) => {
      if (!element.isConnected) {
        state.animation?.cancel(); observer.unobserve(element); motions.delete(element);
      }
    });
    document.querySelectorAll('[data-chonk-motion]').forEach(element => {
      if (motions.has(element)) return;
      const hoverTarget = element.closest('.sidebar-brand');
      const state = {element, animation: null, visible: false, hovered: hoverTarget?.matches(':hover')};
      motions.set(element, state);
      if (element.dataset.chonkMotion === 'hop' && hoverTarget) {
        hoverTarget.addEventListener('pointerenter', () => { state.hovered = true; reconcile(state); });
        hoverTarget.addEventListener('pointerleave', () => { state.hovered = false; reconcile(state); });
      }
      observer.observe(element);
    });
  }

  function render(target, html) {
    if (target.innerHTML === html) return;
    // Keep the same mascot and turn in progress when Activity polls update text.
    const existing = new Map([...target.querySelectorAll('[data-motion-key]')]
      .map(element => [element.dataset.motionKey, element]));
    target.innerHTML = html;
    target.querySelectorAll('[data-motion-key]').forEach(element => {
      const previous = existing.get(element.dataset.motionKey);
      if (previous) element.replaceWith(previous);
    });
    refresh();
  }

  reducedMotion.addEventListener('change', () => motions.forEach(reconcile));
  document.addEventListener('visibilitychange', () => motions.forEach(reconcile));
  window.chonkMotion = {render};
  refresh();
})();
