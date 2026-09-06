// Meeting Saathi public/customer site — vanilla JS, no framework/build step.
// Sections below are additive; each checkpoint of the redesign appends its
// own self-contained block rather than one large controller.

document.documentElement.classList.remove('no-js');

// --- Mobile nav toggle ------------------------------------------------
(function () {
  const toggle = document.querySelector('.nav-toggle');
  const links = document.querySelector('.nav-links');
  if (!toggle || !links) return;
  toggle.addEventListener('click', () => {
    const isOpen = links.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(isOpen));
  });
  links.querySelectorAll('a').forEach((a) =>
    a.addEventListener('click', () => links.classList.remove('open'))
  );
})();

// --- Scroll-reveal ------------------------------------------------------
// Elements with class "reveal" fade/rise into place once they enter the
// viewport. No-op (stays visible, see site.css's reduced-motion rule) if the
// visitor has prefers-reduced-motion set, or if IntersectionObserver isn't
// available at all.
(function () {
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const targets = document.querySelectorAll('.reveal');
  if (!targets.length) return;
  if (prefersReducedMotion || !('IntersectionObserver' in window)) {
    targets.forEach((el) => el.classList.add('is-visible'));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15, rootMargin: '0px 0px -40px 0px' }
  );
  targets.forEach((el) => observer.observe(el));
})();

// --- Hero animated mockup ------------------------------------------------
// Replaceable slot: cycles through Call -> Recording -> Processing ->
// Documents. Swap this whole block (and the .hero-mockup markup) for a
// <video> once a real recording exists -- nothing else on the page depends
// on it.
(function () {
  const frames = document.querySelectorAll('.hero-mockup .mockup-frame');
  if (!frames.length) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    frames[frames.length - 1].classList.add('active');
    return;
  }
  let i = 0;
  setInterval(() => {
    frames[i].classList.remove('active');
    i = (i + 1) % frames.length;
    frames[i].classList.add('active');
  }, 2200);
})();

// --- "See it in action" walkthrough --------------------------------------
(function () {
  const steps = document.querySelectorAll('.walkthrough-step');
  const stageFrames = document.querySelectorAll('.walkthrough-frame');
  if (!steps.length || !stageFrames.length) return;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let current = 0;
  let timer = null;

  function show(index) {
    current = index;
    steps.forEach((s, idx) => s.classList.toggle('active', idx === index));
    stageFrames.forEach((f, idx) => f.classList.toggle('active', idx === index));
  }
  function startAuto() {
    if (reducedMotion) return;
    stopAuto();
    timer = setInterval(() => show((current + 1) % steps.length), 3200);
  }
  function stopAuto() {
    if (timer) clearInterval(timer);
  }
  steps.forEach((step, idx) => {
    step.addEventListener('click', () => { show(idx); stopAuto(); });
  });
  show(0);
  startAuto();
})();

// --- Document tabs (MOM / Meeting Analysis / Business Process Flow) -----
(function () {
  const tabs = document.querySelectorAll('.doc-tab');
  if (!tabs.length) return;
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const key = tab.dataset.docKey;
      tabs.forEach((t) => t.classList.toggle('active', t === tab));
      tabs.forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
      document.querySelectorAll('.doc-preview-body').forEach((body) => {
        body.classList.toggle('active', body.dataset.docKey === key);
      });
      // Mermaid (if this tab's sample contains a diagram) only needs to run
      // once per panel -- re-running on an already-rendered node is a no-op
      // guarded by data-processed, same convention the real document viewer uses.
      const activeBody = document.querySelector(`.doc-preview-body[data-doc-key="${key}"]`);
      if (activeBody && window.mermaid && activeBody.querySelector('pre.mermaid:not([data-processed])')) {
        window.mermaid.run({ querySelector: 'pre.mermaid', suppressErrors: true });
      }
    });
  });
})();

// --- Business Process Flow spotlight: steps -> flowchart animation ------
(function () {
  const anim = document.querySelector('.bpf-anim');
  if (!anim) return;
  const textEl = anim.querySelector('.bpf-steps-text');
  const svg = anim.querySelector('svg');
  if (!svg) return;
  const nodes = svg.querySelectorAll('.bpf-node');
  const edges = svg.querySelectorAll('.bpf-edge');
  const labels = svg.querySelectorAll('.bpf-label');
  let played = false;

  function play() {
    if (played) return;
    played = true;
    if (textEl) textEl.classList.add('fade-out');
    svg.style.display = 'block';
    let delay = textEl ? 400 : 0;
    nodes.forEach((node, i) => {
      setTimeout(() => node.classList.add('show'), delay + i * 220);
    });
    edges.forEach((edge, i) => {
      setTimeout(() => edge.classList.add('show'), delay + 200 + i * 220);
    });
    labels.forEach((label, i) => {
      setTimeout(() => (label.style.opacity = '1'), delay + i * 220);
    });
  }

  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || !('IntersectionObserver' in window)) {
    play();
  } else {
    const obs = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && play()),
      { threshold: 0.4 }
    );
    obs.observe(anim);
  }
})();

// --- FAQ accordion --------------------------------------------------------
(function () {
  document.querySelectorAll('.faq-question').forEach((btn) => {
    btn.addEventListener('click', () => {
      const item = btn.closest('.faq-item');
      const answer = item.querySelector('.faq-answer');
      const isOpen = item.classList.toggle('open');
      btn.setAttribute('aria-expanded', String(isOpen));
      answer.style.maxHeight = isOpen ? answer.scrollHeight + 'px' : '0px';
    });
  });
})();

// --- Pricing currency switch ----------------------------------------------
(function () {
  const select = document.getElementById('pricing-currency');
  if (!select) return;
  function apply(currency) {
    document.querySelectorAll('[data-plan-block]').forEach((block) => {
      block.classList.toggle('active', block.dataset.planBlock === currency);
    });
  }
  select.addEventListener('change', () => apply(select.value));
  apply(select.value);
})();
