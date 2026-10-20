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
      say: "Two windows in the same room can differ by a factor of five in how much street"
         + " pollution they let in. Nobody can tell which is which. SASH measures it, from"
         + " three Ring devices, without ever looking at an image.",
      seconds: 15,
      run: async () => { await tab('now'); await spotlight('.brand', 'SASH'); },
    },
    {
      say: "This is a real decision, for a flat with three openings. Open the courtyard "
         + "window, at this time, for this long.",
      seconds: 8,
      run: async () => { await spotlight('#decision .headline', 'The decision'); },
    },
    {
      say: "The street window this household usually reaches for would admit about half as"
         + " much again, for the same amount of air.",
      seconds: 8,
      run: async () => { await spotlight('#decision .intake', 'Against the habit'); },
    },
    {
      say: "And SASH says how sure it is. Both plans share the same draws, so what is left"
         + " is the difference between the two windows.",
      seconds: 9,
      run: async () => { await spotlight('#decision .confidence', 'How sure'); },
    },
    {
      say: "SASH never changes how much air the home gets. Only when, and through which "
         + "window.",
      seconds: 6,
      run: async () => { await tab('now'); await spotlight('#demand-body', 'Why now'); },
    },
    {
      say: "Carbon monoxide has no street in it. Between cooking events it decays like a "
         + "tracer gas, and how fast it decays with each window open gives that window's "
         + "air exchange rate.",
      seconds: 12,
      run: async () => {
        await tab('windows');
        await spotlight('#openings .opening-card:nth-child(2) .stat:nth-of-type(1)',
                        'Air exchange, from the carbon monoxide channel');
      },
    },
    {
      say: "Tracer gas decay normally needs a technician and a cylinder of gas. This "
         + "household makes the tracer twice a day by cooking, and Ring already measures "
         + "it.",
      seconds: 10,
      run: async () => {
        await spotlight('#openings .opening-card:nth-child(2) .stat:nth-of-type(2)',
                        'Street coupling, the cost');
      },
    },
    {
      say: "And SASH refuses to overclaim. Shuffle which window each airing went through, "
         + "and the courtyard window still wins. The balcony door does not, so SASH says "
         + "these two are not yet distinguishable.",
      seconds: 12,
      run: async () => { await spotlight('#verdict-card', 'The permutation gate'); },
    },
    {
      say: "Now watch the events arrive. Signed webhooks through the shipping receiver, "
         + "and readings through the same client that reads a Ring account.",
      seconds: 8,
      run: async () => {
        await tab('timeline');
        await clickIt('#advance', 'Advance 30 min');
        await waitIdle(3500);
        await spotlight('#view-timeline .card:nth-of-type(2)', 'Events, as delivered');
      },
    },
    {
      say: "A recommendation nobody writes down cannot be wrong later. So SASH publishes "
         + "the plan and its forecast before the airing happens.",
      seconds: 8,
      run: async () => {
        await tab('now');
        // The Advance pressed a beat ago refits the household; Publish is
        // disabled until it has, and a disabled button is not pressed.
        await waitIdle(9000);
        await clickIt('#publish', 'Publish this plan');
        await waitIdle(5000);
        await spotlight('#decision .decision-actions', 'Written down first');
      },
    },
    {
      say: "The household opens it, and the flat is simulated again with that window open.",
      seconds: 5,
      run: async () => {
        await clickIt('#follow', 'The household follows it');
        await waitIdle(8000);
        // Four hours, because a plan cannot be scored until its window and the
        // ninety minutes of recovery after it have both gone past. The beat
        // waits for the work to land, so the next one opens on a scored plan;
        // the recorder gives this beat a hold for the wait and cuts it out.
        await clickIt('#advance-long', 'Advance 4 h');
        window.__SASH_MARKS = { advanced: (Date.now() - window.__SASH_T0) / 1000 };
        await waitIdle(60000);
        window.__SASH_MARKS.landed = (Date.now() - window.__SASH_T0) / 1000;
      },
    },
    {
      say: "Then the plan is scored against the readings that followed. SASH answers only "
         + "for airings it proposed and that actually happened.",
      seconds: 8,
      run: async () => {
        // The advance the previous beat started has to land before the ledger
        // can show a settled plan, because settling happens when the ledger is
        // read. The ledger card is lit while it waits, and measured again once
        // the scored row has landed and the card has grown.
        await tab('record');
        await spotlight('#view-record .card:nth-of-type(2)', 'This session, scored');
        await waitIdle(11000);
        await spotlight('#view-record .card:nth-of-type(2)', 'This session, scored');
      },
    },
    {
      say: "Harder still: fitted on the first part of the record, SASH prices every airing"
         + " in the last part before seeing its cost.",
      seconds: 8,
      run: async () => { await spotlight('#view-record .card:nth-of-type(1)', 'Priced before it happened'); },
    },
    {
      say: "Across twenty-four simulated households it had never seen, SASH beat their own"
         + " habit on sixty-nine of eighty-eight airings, with a median saving of "
         + "forty-six per cent. On forty homes with identical windows, it claimed a "
         + "difference on two.",
      seconds: 15,
      run: async () => { await tab('windows'); await spotlight('#verdict-card', 'What it is right about'); },
    },
  ];

  /* The second part runs on a different household: one that linked its account
   * nine days ago and has just cooked, at /?demo=early on a server started with
   * "sash demo --days 9". Nothing is measured yet, and the reason to air is
   * inside the flat. */
  const EARLY_BEATS = [
    {
      say: "Street air is not always the point. Nine days after linking, this household "
         + "has just cooked. When the reason to open a window is inside the flat, SASH "
         + "stops optimising for the street, and only picks the window.",
      seconds: 15,
      run: async () => { await tab('now'); await spotlight('#decision', 'Nine days in, just after cooking'); },
    },
    {
      say: "It picks one it has not measured yet, because the fumes fading through it are "
         + "the measurement.",
      seconds: 7,
      run: async () => { await spotlight('#decision p:last-of-type', 'The fumes are the measurement'); },
    },
    {
      say: "Each window shows its progress. Guided like this, simulated homes had every "
         + "window measured in a median of eleven days, instead of forty.",
      seconds: 9,
      run: async () => { await spotlight('#calibration-card', 'Getting to know your windows'); },
    },
    {
      say: "All of it from a camera it never looks through.",
      seconds: 4,
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

  /** Play one part of the walkthrough. `main` is the 120-day household and
   * `early` the household nine days in; the recorder films each against its own
   * server and joins them, so the progress bar counts across both. */
  async function play(part = 'main') {
    // Wall-clock start, read back by the recorder to cut the page load off.
    window.__SASH_T0 = Date.now();
    mount();
    const beats = part === 'early' ? EARLY_BEATS : BEATS;
    const offset = part === 'early' ? BEATS.length : 0;
    const total = BEATS.length + EARLY_BEATS.length;
    const timing = window.__SASH_TIMING || null;
    // Every beat is due at a fixed offset from the first, the way its sentence
    // is in the narration. A beat that overruns (a click waiting on the
    // product) is absorbed by the next one rather than pushing every later
    // beat behind the voice.
    const clock = performance.now();
    let due = 0;
    for (beatIndex = 0; beatIndex < beats.length; beatIndex++) {
      const beat = beats[beatIndex];
      const seconds = timing ? (timing[beatIndex] || beat.seconds) : beat.seconds;
      const ahead = clock + due * 1000 - performance.now();
      if (ahead > 0) await sleep(ahead);
      due += seconds;
      caption(beat.say);
      progress.style.width = `${((offset + beatIndex + 1) / total) * 100}%`;
      try { await beat.run(); } catch (e) { console.error('beat', beatIndex, e); }
      window.__SASH_BEAT = beatIndex + 1;
    }
    const rest = clock + due * 1000 - performance.now();
    if (rest > 0) await sleep(rest);
    unspot();
    hideCursor();
    // A recorded first part cuts straight to the second, so only the last part
    // (or a live viewing) ends on the title.
    if (part !== 'main' || !window.__SASH_TIMING) caption('SASH. Street Air, Sensed at Home.');
    window.__SASH_DONE = true;
  }

  const list = bs => bs.map(b => ({ say: b.say, seconds: b.seconds }));
  return { play, beats: () => list(BEATS), earlyBeats: () => list(EARLY_BEATS) };
})();

window.SashDemo = DEMO;

/* Auto-play at /?demo=1, or /?demo=early for the second part, once the first
 * refresh has painted real numbers. */
if (new URLSearchParams(location.search).has('demo')) {
  const part = new URLSearchParams(location.search).get('demo') === 'early' ? 'early' : 'main';
  const start = () => {
    if (document.querySelector('#decision .headline')) DEMO.play(part);
    else setTimeout(start, 250);
  };
  setTimeout(start, 700);
}
