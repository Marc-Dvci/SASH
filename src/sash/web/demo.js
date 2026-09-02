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
      run: async () => { await tab('now'); await spotlight('#demand-card', 'Why now'); },
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
      say: "Now watch Ring deliver. Every event here is a signed webhook through the "
         + "shipping receiver, and every reading is fetched by the same client that would "
         + "call Ring.",
      seconds: 10,
      run: async () => {
        await tab('timeline');
        await clickIt('#advance', 'Advance');
        await sleep(400);
      },
    },
    {
      say: "The readings arrive, the model refits, and the plan moves with them.",
      seconds: 6,
      run: async () => { await spotlight('#timeline', 'Live readings'); },
    },
    {
      say: "A recommendation nobody writes down cannot be wrong later. So SASH publishes "
         + "the plan, with its forecast and its interval, before the airing happens.",
      seconds: 10,
      run: async () => {
        await tab('now');
        await clickIt('#publish', 'Publish this plan');
        await sleep(600);
      },
    },
    {
      say: "The household opens it. The flat is re-integrated with that window open, on "
         + "the weather and the street that were already drawn, so the readings afterwards "
         + "are what it would really have measured.",
      seconds: 11,
      run: async () => {
        await clickIt('#follow', 'The household follows it');
        await sleep(900);
        // Four hours, because a plan cannot be scored until its window and the
        // ninety minutes of recovery after it have both gone past.
        await clickIt('#advance-long', 'Advance 4 h');
      },
    },
    {
      say: "And it gets scored against readings it genuinely produced. SASH takes the "
         + "credit and the blame only for airings it proposed and that actually happened.",
      seconds: 10,
      run: async () => {
        await clickIt('#advance-long', 'Advance 4 h');
        await tab('record');
        await spotlight('#ledger', 'The ledger');
      },
    },
    {
      say: "Above it, the harder version. The model is fitted on the first part of this "
         + "home's record and asked to price every airing in the last part before seeing "
         + "what it cost.",
      seconds: 11,
      run: async () => { await spotlight('#backtest', 'Priced before it happened'); },
    },
    {
      say: "Across twenty-four simulated households it has never seen, SASH beat the "
         + "household's own habit on seventy-one of ninety-four airings. On forty homes "
         + "whose windows are physically identical, it announced a difference on two, at "
         + "a nominal five per cent.",
      seconds: 15,
      run: async () => { await spotlight('#verdict-card', 'What it is right about'); await tab('windows'); },
    },
    {
      say: "All of it from a camera it never looks through. The Ring client carries a "
         + "path allowlist, and the test suite walks every media endpoint against a server "
         + "that would have answered.",
      seconds: 12,
      run: async () => { await tab('setup'); await spotlight('.privacy', 'What SASH never asks for'); },
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

  async function tab(name) {
    const button = document.querySelector(`#tabs button[data-view="${name}"]`);
    if (button && !button.classList.contains('active')) {
      await moveCursorTo(button);
      button.click();
      await sleep(320);
    }
  }

  async function moveCursorTo(el) {
    if (!el) return;
    const box = el.getBoundingClientRect();
    cursor.hidden = false;
    cursor.style.transform =
      `translate(${box.left + box.width / 2}px, ${box.top + box.height / 2}px)`;
    await sleep(360);
  }

  async function clickIt(selector, label) {
    const el = $(selector);
    if (!el || el.hidden || el.disabled) return;
    await moveCursorTo(el);
    el.classList.add('demo-press');
    el.click();
    await sleep(260);
    el.classList.remove('demo-press');
  }

  /** Put the ring of light on a real element, scrolling it into view first. */
  async function spotlight(selector, label) {
    const el = $(selector);
    if (!el) { spot.hidden = true; return; }
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    await sleep(420);
    const box = el.getBoundingClientRect();
    const pad = 10;
    spot.hidden = false;
    spot.style.left = `${box.left - pad}px`;
    spot.style.top = `${box.top - pad}px`;
    spot.style.width = `${box.width + pad * 2}px`;
    spot.style.height = `${box.height + pad * 2}px`;
    spot.dataset.label = label || '';
    await moveCursorTo(el);
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
    spot.hidden = true;
    cursor.hidden = true;
    caption('SASH — Street Air, Sensed at Home.');
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
