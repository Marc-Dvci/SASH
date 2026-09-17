/* The guided walkthrough, at /?demo=1.
 *
 * It drives the real product: every beat clicks the same buttons a person
 * clicks, and every number that appears came from the API. Nothing here draws a
 * fake screen or a fake number, which is why the recording in demo_video/ is a
 * recording of SASH rather than an animation of it.
 *
 * Timing comes from window.__SASH_TIMING when the recorder injects it, so the
 * visuals pace themselves to the narration and both share one clock. Without it
 * each beat uses its own default and the walkthrough is still watchable live.
 */

const DEMO = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  let layer, captionEl, spot, cursor, progress;
  let beatIndex = 0;

  /* ---------------------------------------------------------------- beats */

  /* Each beat is { say, seconds, run }. `say` is spoken by narrate.py and burnt
   * in as a caption, word for word, so the video ships with subtitles and no
   * edit pass. `run` does the clicking. */
  const BEATS = [
    {
      say: "Two windows in the same room can differ by a factor of five in how much "
         + "street pollution they let in. Nobody can tell which is which. SASH measures it, "
         + "from three Ring devices, without ever looking at an image.",
      seconds: 12,
      run: async () => { await tab('now'); await spotlight('.brand', 'SASH'); },
    },
    {
      say: "This is a real decision, taken now, for a flat with three openings. "
         + "Open the courtyard window, at this time, for this long.",
      seconds: 8,
      run: async () => { await spotlight('#decision .headline', 'The decision'); },
    },
    {
      say: "It is compared against what this household actually does. The street window "
         + "it usually reaches for would admit about half as much again, for the same "
         + "amount of air.",
      seconds: 10,
      run: async () => { await spotlight('#decision .intake', 'Against the habit'); },
    },
    {
      say: "And SASH says how sure it is. Both plans are forecast together on each "
         + "bootstrap draw, so they share the same weather and the same day, and what is "
         + "left is the difference between the two windows.",
      seconds: 11,
      run: async () => { await spotlight('#decision .confidence', 'How sure'); },
    },
    {
      say: "SASH never changes how much air the home gets. The target is this "
         + "household's own median. It moves when you air, and which window, and nothing "
         + "else.",
      seconds: 9,
      run: async () => { await tab('now'); await spotlight('#demand-body', 'Why now'); },
    },
    {
      say: "Here is the measurement that makes it possible. Carbon monoxide has no "
         + "street in it. Between cooking events it is a decaying tracer, so regressing "
         + "its decay rate on how open each window was gives that window's air exchange "
         + "rate directly.",
      seconds: 14,
      run: async () => {
        await tab('windows');
        await spotlight('#openings .opening-card:nth-child(2) .stat:nth-of-type(1)',
                        'Air exchange, from the carbon monoxide channel');
      },
    },
    {
      say: "That is tracer gas decay, which normally needs a technician and a cylinder of "
         + "gas. This household makes the tracer twice a day by cooking, and Ring already "
         + "measures it.",
      seconds: 10,
      run: async () => {
        await spotlight('#openings .opening-card:nth-child(2) .stat:nth-of-type(2)',
                        'Street coupling, the cost');
      },
    },
    {
      say: "And SASH refuses to over-claim. Every airing is held as it happened and only "
         + "the label is shuffled. The courtyard window survives that. The balcony door "
         + "does not, so SASH says these two are not yet distinguishable.",
      seconds: 12,
      run: async () => { await spotlight('#verdict-card', 'The permutation gate'); },
    },
    {
      say: "Now watch the events arrive. Every one is a signed webhook through the "
         + "shipping receiver, and every reading is fetched by the same client that "
         + "reads a Ring account.",
      seconds: 10,
      run: async () => {
        await tab('timeline');
        await clickIt('#advance', 'Advance 30 min');
        await waitIdle(3500);
        await spotlight('#view-timeline .card:nth-of-type(2)', 'Events, as delivered');
      },
    },
    {
      say: "The readings arrive, the model refits, and the plan moves with them.",
      seconds: 6,
      run: async () => { await spotlight('#view-timeline .card:nth-of-type(1)', 'Live readings'); },
    },
    {
      say: "A recommendation nobody writes down cannot be wrong later. So SASH publishes "
         + "the plan, with its forecast and its interval, before the airing happens.",
      seconds: 10,
      run: async () => {
        await tab('now');
        await clickIt('#publish', 'Publish this plan');
        await waitIdle(5000);
        await spotlight('#decision .decision-actions', 'Written down first');
      },
    },
    {
      say: "The household opens it. The flat is re-integrated with that window open, on "
         + "the weather and the street that were already drawn, so the readings afterwards "
         + "are what it would really have measured.",
      seconds: 11,
      run: async () => {
        await clickIt('#follow', 'The household follows it');
        await waitIdle(8000);
        // Four hours, because a plan cannot be scored until its window and the
        // ninety minutes of recovery after it have both gone past. The click
        // returns immediately; the next beat waits for the work.
        await clickIt('#advance-long', 'Advance 4 h');
      },
    },
    {
      say: "And it gets scored against the readings that followed. SASH takes the "
         + "credit and the blame only for airings it proposed and that actually happened.",
      seconds: 10,
      run: async () => {
        // The advance the previous beat started has to land before the ledger
        // can show a settled plan, because settling happens when the ledger is
        // read. The ledger card is lit while it waits, and measured again once
        // the scored row has landed and the card has grown.
        await tab('record');
        await spotlight('#view-record .card:nth-of-type(2)', 'This session, scored');
        await waitIdle(7500);
        await spotlight('#view-record .card:nth-of-type(2)', 'This session, scored');
      },
    },
    {
      say: "Above it, the harder version. The model is fitted on the first part of this "
         + "home's record and asked to price every airing in the last part before seeing "
         + "what it cost.",
      seconds: 11,
      run: async () => { await spotlight('#view-record .card:nth-of-type(1)', 'Priced before it happened'); },
    },
    {
      say: "Across twenty-four simulated households it has never seen, SASH beat the "
         + "household's own habit on seventy-one of ninety-four airings. On forty homes "
         + "whose windows are physically identical, it announced a difference on two, at "
         + "a nominal five per cent.",
      seconds: 15,
      run: async () => { await tab('windows'); await spotlight('#verdict-card', 'What it is right about'); },
    },
    {
      say: "All of it from a camera it never looks through. The Ring client carries a "
         + "path allowlist, and the test suite walks every media endpoint against a server "
         + "that would have answered.",
      seconds: 12,
      run: async () => { await tab('setup'); await spotlight('#view-setup .card:nth-of-type(3)', 'What SASH never asks for'); },
    },
  ];

  /* ------------------------------------------------------------ machinery */

  function mount() {
    layer = document.createElement('div');
    layer.className = 'demo-layer';
    layer.innerHTML = `
      <div class="demo-spot" hidden></div>
      <div class="demo-cursor" hidden></div>
      <div class="demo-caption"><p></p></div>
      <div class="demo-progress"><i></i></div>`;
    document.body.appendChild(layer);
    document.body.classList.add('demo-running');
    spot = $('.demo-spot', layer);
    cursor = $('.demo-cursor', layer);
    captionEl = $('.demo-caption p', layer);
    progress = $('.demo-progress i', layer);
  }

  function caption(text) {
    captionEl.textContent = text;
    captionEl.parentElement.classList.remove('demo-fade');
    void captionEl.offsetWidth;
    captionEl.parentElement.classList.add('demo-fade');
  }

  /** The band of the viewport a viewer can actually see: below the sticky
   * header and tabs, above the caption. Everything the walkthrough points at
   * is scrolled into this band and the ring of light is clipped to it. */
  function safeZone() {
    const tabs = $('#tabs');
    const cap = $('.demo-caption', layer);
    const top = (tabs ? tabs.getBoundingClientRect().bottom : 0) + 8;
    const bottom = (cap ? cap.getBoundingClientRect().top : window.innerHeight) - 12;
    return { top, bottom };
  }

  /** Scroll only as far as it takes for `el` to sit inside the safe zone.
   *
   * A target that is already visible stays put, one just below the fold moves
   * up by the minimum, and one taller than the band is aligned to the top of
   * it. Re-centring the page on every beat reads as the screen lurching. */
  async function reveal(el) {
    const zone = safeZone();
    const box = el.getBoundingClientRect();
    let delta = 0;
    if (box.height > zone.bottom - zone.top) delta = box.top - zone.top;
    else if (box.top < zone.top) delta = box.top - zone.top;
    else if (box.bottom > zone.bottom) delta = box.bottom - zone.bottom;
    if (Math.abs(delta) < 4) return;
    window.scrollBy({ top: delta, behavior: 'smooth' });
    // Wait for the smooth scroll to settle rather than for a fixed time.
    let last = -1, still = 0;
    const until = performance.now() + 900;
    while (performance.now() < until && still < 3) {
      await sleep(40);
      if (window.scrollY === last) still++; else { still = 0; last = window.scrollY; }
    }
  }

  async function tab(name) {
    const button = document.querySelector(`#tabs button[data-view="${name}"]`);
    if (button && !button.classList.contains('active')) {
      // A new tab is a new picture; nothing from the old one may stay lit.
      unspot();
      await moveCursorTo(button);
      button.click();
      await sleep(320);
      hideCursor();
    }
  }

  async function moveCursorTo(el) {
    if (!el) return;
    const box = el.getBoundingClientRect();
    cursor.hidden = false;
    void cursor.offsetWidth;
    cursor.classList.add('on');
    cursor.style.transform =
      `translate(${box.left + box.width / 2}px, ${box.top + box.height / 2}px)`;
    await sleep(360);
  }

  /** The cursor exists to show a press. Between presses it is off screen, so
   * it never sits on a number the narration is reading out. */
  function hideCursor() {
    cursor.classList.remove('on');
  }

  /** Wait until the page has finished refetching and refitting.
   *
   * Pressing Advance delivers a few hundred webhooks and then refits the whole
   * household, and on a slow machine that is longer than the beat that pressed
   * it. Waiting on the product's own busy flag keeps the visuals behind the
   * voice instead of ahead of it, and caps the wait so a stall cannot eat the
   * rest of the walkthrough. */
  async function waitIdle(maxMs = 9000) {
    const until = performance.now() + maxMs;
    while (performance.now() < until) {
      const busy = document.body.dataset.busy
        || $('#advance-long')?.disabled
        || $('#advance')?.disabled;
      if (!busy) return true;
      await sleep(200);
    }
    return false;
  }

  /** Press a button the viewer can see being pressed: the ring of light goes
   * on the button itself, the cursor arrives, the button flashes. */
  async function clickIt(selector, label) {
    const el = $(selector);
    if (!el || el.hidden || el.disabled) return;
    await spotlight(selector, label, { pad: 6 });
    await moveCursorTo(el);
    el.classList.add('demo-press');
    el.click();
    await sleep(260);
    el.classList.remove('demo-press');
    await sleep(300);
    hideCursor();
  }

  function unspot() {
    spot.hidden = true;
    spot.classList.remove('label-inside', 'label-below');
  }

  /** Put the ring of light on a real element, scrolling it into view first.
   *
   * The ring is clipped to the safe zone, so a card taller than the screen is
   * lit from its top edge down to the caption rather than spilling under the
   * sticky header. The label sits on a plate above the ring, or just inside
   * its top edge when there is no room above. */
  async function spotlight(selector, label, { pad = 10 } = {}) {
    const el = $(selector);
    if (!el) { unspot(); return; }
    hideCursor();
    await reveal(el);
    const zone = safeZone();
    const box = el.getBoundingClientRect();
    const left = Math.max(6, box.left - pad);
    const right = Math.min(window.innerWidth - 6, box.right + pad);
    const top = Math.max(zone.top, box.top - pad);
    const bottom = Math.min(zone.bottom, box.bottom + pad);
    spot.hidden = false;
    spot.style.left = `${left}px`;
    spot.style.top = `${top}px`;
    spot.style.width = `${right - left}px`;
    spot.style.height = `${Math.max(24, bottom - top)}px`;
    spot.dataset.label = label || '';
    // No room above: below the ring if that is visible, otherwise inside it.
    const above = top - 26 >= zone.top;
    const below = !above && bottom + 30 <= zone.bottom;
    spot.classList.toggle('label-below', below);
    spot.classList.toggle('label-inside', !above && !below);
  }

  async function play() {
    mount();
    const timing = window.__SASH_TIMING || null;
    for (beatIndex = 0; beatIndex < BEATS.length; beatIndex++) {
      const beat = BEATS[beatIndex];
      const seconds = timing ? (timing[beatIndex] || beat.seconds) : beat.seconds;
      const started = performance.now();
      caption(beat.say);
      progress.style.width = `${((beatIndex + 1) / BEATS.length) * 100}%`;
      try { await beat.run(); } catch (e) { console.error('beat', beatIndex, e); }
      const spent = (performance.now() - started) / 1000;
      if (spent < seconds) await sleep((seconds - spent) * 1000);
      window.__SASH_BEAT = beatIndex + 1;
    }
    unspot();
    hideCursor();
    caption('SASH. Street Air, Sensed at Home.');
    window.__SASH_DONE = true;
  }

  return { play, beats: () => BEATS.map(b => ({ say: b.say, seconds: b.seconds })) };
})();

window.SashDemo = DEMO;

/* Auto-play at /?demo=1, once the first refresh has painted real numbers. */
if (new URLSearchParams(location.search).has('demo')) {
  const start = () => {
    if (document.querySelector('#decision .headline')) DEMO.play();
    else setTimeout(start, 250);
  };
  setTimeout(start, 700);
}
