/* SASH front end.
 *
 * No framework and no build step: five views, each one a pure function from a
 * JSON payload to DOM. Everything on screen is a number the model produced, and
 * every number that has an interval is drawn with it. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = { view: 'now', hours: 24, busy: false, canFollow: false };

async function get(path)  { const r = await fetch(path); if (!r.ok) throw new Error(path + ' ' + r.status); return r.json(); }
async function post(path) { const r = await fetch(path, { method: 'POST' }); return r.json(); }

const nf  = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? '--' : Number(v).toFixed(d);

/* Every time on this page is UTC, because every time the server reasoned about
 * was UTC. A household's SASH runs in the household's own zone; rendering the
 * browser's zone over a server that used another one is how a headline ends up
 * two hours away from the sentence under it. */
const UTC = { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false };
const hhmm  = t => new Date(t * 1000).toLocaleTimeString('en-GB', UTC);
const dayHM = t => new Date(t * 1000).toLocaleString('en-GB', { ...UTC, weekday: 'short' });
const bandClass = b => ({ 'low': 'quiet', 'typical': '', 'high': 'busy', 'very high': 'very-busy' }[b] ?? '');

function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

/* ============================ live strip ============================ */

function renderLive(s) {
  const i = s.indoor, st = s.street;
  const openNow = s.openings.filter(o => o.state === 'open');
  const cells = [
    { k: 'Indoor PM2.5', v: nf(i.pm25, 1), u: 'µg/m³', alert: i.pm25_alert },
    { k: 'CO',           v: nf(i.co, 2),   u: 'ppm',   alert: i.co_alert },
    { k: 'Humidity',     v: nf(i.humidity, 0), u: '%' },
    { k: 'Indoors',      v: nf(i.temperature, 1), u: '°C' },
    { k: 'Street',       v: st.events_last_30min, u: 'events / 30 min' },
    { k: 'Open now',     v: openNow.length ? openNow.map(o => o.label).join(', ') : 'nothing', u: '' },
  ];
  $('#live-strip').innerHTML = cells.map(c => `
    <div class="metric ${c.alert ? 'alert' : ''}">
      <span class="k">${c.k}</span>
      <span class="v">${c.v}${c.u ? `<small>${c.u}</small>` : ''}</span>
    </div>`).join('');

  $('#synthetic-banner').hidden = !s.synthetic_history;
  const age = i.age_minutes;
  $('#footer-note').textContent =
    `Air Quality Monitor last reported ${age === null ? 'never' : nf(age, 0) + ' minutes ago'}` +
    ` · the device reports about every 15 minutes and the status endpoint returns that last reading, not a live one` +
    ` · ${st.total_events.toLocaleString()} vehicle-motion events on record`;
}

/* ============================ NOW ============================ */

function bar(lo, hi, point, scale, cls) {
  const pct = v => Math.max(0, Math.min(100, (v / scale) * 100));
  return `<div class="bar ${cls}">
      <div class="track"></div>
      <div class="span" style="left:${pct(lo)}%;width:${Math.max(1.2, pct(hi) - pct(lo))}%"></div>
      <div class="point" style="left:${pct(point)}%"></div>
    </div>`;
}

function renderDecision(rec) {
  const el = $('#decision');
  const best = rec.best;
  if (!best) {
    el.className = 'card decision';
    el.innerHTML = `<div class="eyebrow">The decision</div>
      <p class="headline" style="font-size:21px">SASH is not ready to rank your openings.</p>
      <p class="sub">${rec.reason}</p>`;
    return;
  }

  const habit = rec.habit;
  const scale = Math.max(best.pred_intake, habit ? habit.pred_intake : 0, 1) * 1.25;
  const narrative = rec.narrative || {};
  const halfWidth = 0.5 * (best.pred_intake_hi - best.pred_intake_lo);

  el.className = 'card decision' + (best.exploration ? ' explore' : '');
  el.innerHTML = `
    <div class="eyebrow">The decision</div>
    ${best.exploration ? '<div class="explore-flag">Deliberate experiment</div>' : ''}
    <p class="headline">
      Open the <span class="opening">${best.opening_label}</span>
      at <span class="when">${hhmm(best.start_at)}</span>
      for <span class="when">${nf(best.duration_min)}</span> minutes.
    </p>
    <p class="sub">${narrative.text || rec.reason}</p>

    <div class="intake">
      <div class="row">
        <span class="name">This plan</span>
        ${bar(0, best.pred_intake, best.pred_intake, scale, 'best')}
        <span class="num">${nf(best.pred_intake)}</span>
      </div>
      ${habit ? `<div class="row">
        <span class="name">Your usual airing</span>
        ${bar(0, habit.pred_intake, habit.pred_intake, scale, 'habit')}
        <span class="num">${nf(habit.pred_intake)}</span>
      </div>` : ''}
      <div class="row">
        <span class="name" style="color:var(--ink-faint)">µg/m³ × minutes admitted above a closed home,
        for ${nf(rec.demand.target_exchanges, 1)} air changes either way</span>
      </div>
    </div>

    ${rec.saving_pct !== null && rec.saving_pct > 0 && !best.exploration ? `
      <div class="saving">
        <b>${nf(rec.saving_pct)}%</b>
        <span>less particle load than the ${habit.opening_label}, for the same amount of air${
          rec.saving_lo !== null && rec.saving_hi !== null
            ? ` · 90% of paired draws fall between ${nf(rec.saving_lo)}% and ${nf(rec.saving_hi)}%` : ''}</span>
      </div>` : ''}

    ${rec.p_better !== null && rec.p_better !== undefined ? `
      <div class="confidence">
        <div class="conf-track"><div class="conf-fill" style="width:${rec.p_better * 100}%"></div></div>
        <span><b>${nf(rec.p_better * 100)}%</b> of ${rec.n_draws} paired draws have this plan costing less
        than the habit. Both plans are forecast together on each draw, so they share the same weather and the
        same day, and what is left is the difference between the two openings.</span>
      </div>` : ''}

    <div class="decision-actions">
      <button class="solid" id="publish">Publish this plan</button>
      <button class="ghost" id="follow" ${state.canFollow ? '' : 'hidden'}>The household follows it</button>
      <span class="said">Publishing writes the forecast down so the readings afterwards can score it.
        SASH does not open windows: a plan nobody acts on is recorded as exactly that.</span>
    </div>

    <div class="narrative-src">
      Single-airing interval ±${nf(halfWidth)} µg/m³ × min at 90%, widened
      ${nf((rec.calibration || {}).k, 1)}× from the parameter bootstrap by
      ${(rec.calibration || {}).n || 0} of this home's own past airings.
      One airing is hard to price and hard to measure: on the benchmark the median forecast
      error is 52% of a median airing, and the retrospective measurement carries its own. The comparison
      above is the claim, because it is a difference between two openings on the same day and
      the terms that make a single number uncertain are common to both.
      · Wording: <code>${narrative.source || 'deterministic'}</code>
    </div>`;

  $('#publish').onclick = async () => {
    $('#publish').disabled = true;
    const row = await post('/api/plan');
    toast(row.plan_id ? 'Plan published to the record.' : 'Nothing to publish yet.');
    refresh();
  };

  const followBtn = $('#follow');
  if (followBtn) followBtn.onclick = async () => {
    followBtn.disabled = true;
    followBtn.textContent = 'Re-running the flat…';
    try {
      const r = await post('/api/demo/follow');
      toast(r.ok
        ? `The ${r.opening} will open. ${r.readings_rebuilt} later readings re-measured. Now press Advance.`
        : (r.detail || r.reason || 'Publish a plan first.'));
      await refresh();
    } finally {
      followBtn.disabled = false;
      followBtn.textContent = 'The household follows it';
    }
  };
}

function renderDemand(rec, status) {
  const d = rec.demand;
  const i = status.indoor;
  const pct = Math.max(0, Math.min(100, 100 * d.delivered_12h / Math.max(d.target_exchanges, 1e-6)));
  $('#demand-body').className = '';
  $('#demand-body').innerHTML = `
    <div class="demand-level ${d.level}">${
      d.level === 'overdue' ? 'Airing overdue' : d.level === 'due' ? 'Airing due' : 'No airing needed'
    }</div>
    ${d.drivers.length
      ? `<ul class="driver-list">${d.drivers.map(x => `<li>${x}</li>`).join('')}</ul>`
      : '<p class="hint" style="margin:0">This home has had about as much air as it usually gets.</p>'}
    <div class="gauge">
      <div class="label">
        <span>Air changes in the last 12 hours</span>
        <span>${nf(d.delivered_12h, 1)} of ${nf(d.target_exchanges, 1)}</span>
      </div>
      <div class="track"><div class="fill" style="width:${pct}%"></div></div>
    </div>
    <div class="readings">
      <div><span class="k">Indoor PM2.5</span><span class="v">${nf(i.pm25, 1)}<small>µg/m³</small></span>
        <span class="m">this flat settles at ${nf(status.sealed_floor, 1)}</span></div>
      <div><span class="k">Humidity</span><span class="v">${nf(i.humidity, 0)}<small>%</small></span>
        <span class="m">${d.humidity !== null ? 'measured now' : 'not reporting'}</span></div>
      <div><span class="k">Carbon monoxide</span><span class="v">${nf(i.co, 2)}<small>ppm</small></span>
        <span class="m">alert above ${nf(status.co_alert_ppm, 0)}</span></div>
      <div><span class="k">Street now</span><span class="v">${status.street.events_last_30min}<small>/30 min</small></span>
        <span class="m">${nf(status.street.percentile)}th percentile for this camera</span></div>
    </div>
    <p class="hint" style="margin-top:14px">
      The target is this household's own historical median, not a guideline. SASH moves
      <em>when</em> and <em>which</em>, never <em>how much</em>.
    </p>`;
}

function renderAlternatives(rec) {
  const cheapest = [rec.best, ...(rec.alternatives || [])]
    .filter(Boolean)
    .reduce((a, b) => (b.pred_intake < a.pred_intake ? b : a));
  $('#alternatives-hint').textContent =
    `Every opening at every quarter hour over the next twelve hours, forecast one by one. ` +
    `${rec.n_considered} plans survived the only filter SASH applies to the clock: it will not offer ` +
    `a time this household has never aired at. Over ${rec.n_airings_observed} airings, that is ` +
    `${nf(rec.habit_hours, 1)} hours of the day.`;
  const rows = [];
  if (rec.best) rows.push({ ...rec.best, chosen: true });
  (rec.alternatives || []).forEach(a => rows.push(a));
  if (rec.habit) rows.push({ ...rec.habit, isHabit: true });

  $('#alternatives').className = '';
  $('#alternatives').innerHTML = `
    <table class="alts">
      <thead><tr>
        <th>Opening</th><th>Start</th><th class="num">Minutes</th>
        <th>Street then</th><th class="num">Intake</th>
      </tr></thead>
      <tbody>${rows.map(r => `
        <tr class="${r.chosen ? 'chosen' : ''}">
          <td>${r.opening_label}${r.isHabit ? ' <span class="band">habit</span>' : ''}${
            !r.chosen && !r.isHabit && r.plan_id === cheapest.plan_id
              ? ' <span class="band quiet">cheapest</span>' : ''}</td>
          <td class="num">${hhmm(r.start_at)}</td>
          <td class="num">${nf(r.duration_min)}</td>
          <td><span class="band ${bandClass(r.activity_band)}">${r.activity_band}</span></td>
          <td class="num">${nf(r.pred_intake)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function renderStreetProfile(act) {
  const pts = act.now_is_weekday ? act.weekday : act.weekend;
  const other = act.now_is_weekday ? act.weekend : act.weekday;
  if (!pts.length) return;
  const w = 420, h = 108, pad = 4, base = h - 16;
  const max = Math.max(...pts, ...other, 0.05);
  const bw = (w - pad * 2) / pts.length;
  const bars = pts.map((v, i) => {
    const bh = (v / max) * (base - 6);
    return `<rect x="${pad + i * bw}" y="${base - bh}" width="${Math.max(bw - 1, 1)}" height="${Math.max(bh, 0.5)}"
             fill="var(--street-deep)" opacity="${i === act.now_slot ? '1' : '0.8'}"/>`;
  }).join('');
  const line = other.map((v, i) =>
    `${i ? 'L' : 'M'}${(pad + i * bw + bw / 2).toFixed(1)},${(base - (v / max) * (base - 6)).toFixed(1)}`).join('');
  const ticks = [0, 12, 24, 36].map(i =>
    `<text x="${pad + i * bw}" y="${h - 3}" fill="var(--ink-faint)" font-size="9">${String(i / 2).padStart(2, '0')}:00</text>`).join('');
  const cursor = `<line x1="${pad + act.now_slot * bw + bw / 2}" y1="4" x2="${pad + act.now_slot * bw + bw / 2}" y2="${base}"
      stroke="var(--air)" stroke-width="1" stroke-dasharray="2 2"/>`;
  $('#street-profile').innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px">
      ${bars}${cursor}
      <path d="${line}" fill="none" stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="3 2"/>
      <line x1="${pad}" y1="${base}" x2="${w - pad}" y2="${base}" stroke="var(--line)" stroke-width="1"/>
      ${ticks}
    </svg>
    <p class="hint" style="margin:6px 0 0">
      Bars: a ${act.now_is_weekday ? 'weekday' : 'weekend day'}, the median of
      ${act.n_events.toLocaleString()} vehicle-motion events by half hour.
      Dashed line: the other kind of day. The teal marker is now.
    </p>`;
}

/* ============================ WINDOWS ============================ */

function ciBar(lo, hi, point, scale, cls) {
  const pct = v => Math.max(0, Math.min(100, (v / scale) * 100));
  return `<div class="ci-bar ${cls}">
      <div class="axis"></div>
      <div class="rng" style="left:${pct(lo)}%;width:${Math.max(1.2, pct(hi) - pct(lo))}%"></div>
      <div class="dot" style="left:${pct(point)}%"></div>
    </div>`;
}

function renderOpenings(model) {
  const openings = Object.values(model.model.openings);
  const maxTheta = Math.max(...openings.map(o => o.exchange_ci[1] || o.exchange), 1) * 1.1;
  const maxG     = Math.max(...openings.map(o => o.coupling_ci[1] || o.coupling), 1) * 1.1;

  $('#openings').innerHTML = openings.map(o => `
    <article class="card opening-card ${o.reported ? '' : 'unreported'}">
      <p class="name">${o.label}</p>
      <span class="orient">${o.orientation || 'unmapped'} side</span>

      ${o.reported ? `
      <div class="stat">
        <div class="k">Air exchange</div>
        <div class="v">${nf(o.exchange, 1)}<small> air changes / hour</small></div>
        <div class="ci">90% interval ${nf(o.exchange_ci[0], 1)} to ${nf(o.exchange_ci[1], 1)}</div>
        ${ciBar(o.exchange_ci[0], o.exchange_ci[1], o.exchange, maxTheta, 'air')}
        <div class="meaning">Half the room's air replaced in about ${nf(o.refresh_minutes)} minutes.
          Measured by carbon-monoxide decay over ${o.n_tracer} airings.</div>
      </div>

      <div class="stat">
        <div class="k">Street coupling</div>
        <div class="v">${nf(o.coupling, 1)}<small> µg/m³ per event / min</small></div>
        <div class="ci">90% interval ${nf(o.coupling_ci[0], 1)} to ${nf(o.coupling_ci[1], 1)}</div>
        ${ciBar(o.coupling_ci[0], o.coupling_ci[1], o.coupling, maxG, 'street')}
        <div class="meaning">What one extra vehicle-motion event per minute adds to the air
          arriving here, above the one quiet-street background this home shares across its
          openings.</div>
      </div>

      <div class="stat">
        <div class="k">Evidence</div>
        <div class="meaning">${o.n_intervals} readings across ${o.n_episodes} separate airings
          through this opening alone.</div>
      </div>
      ` : `<div class="learning">${o.reason}</div>`}
    </article>`).join('');
}

function renderVerdicts(model) {
  const cs = model.comparisons;
  const el = $('#verdicts');
  el.className = '';
  if (!cs.length) {
    el.innerHTML = `<p class="hint" style="margin:0">SASH needs at least two measured openings
      before it will compare them.</p>`;
    return;
  }
  el.innerHTML = `
    <p class="hint">Each airing is held exactly as it happened and only the label — which
    opening it belonged to — is shuffled. If the difference SASH found survives that,
    it is a difference between the openings. If it does not, SASH says so.</p>
    ${cs.map(c => `
      <div class="verdict ${c.significant ? 'sig' : 'notsig'}">
        <span class="p">p = ${c.p_value.toFixed(3)}</span>
        <span class="txt">${c.verdict}.
          <span style="color:var(--ink-faint)">Gap of ${nf(c.coupling_gap, 1)} µg/m³ per event/min
          between the ${c.a_label} and the ${c.b_label}, over
          ${c.n_episodes_a} and ${c.n_episodes_b} airings, against ${c.n_permutations} shuffles.</span>
        </span>
      </div>`).join('')}`;
}

function renderHome(model) {
  const m = model.model;
  $('#home-body').className = '';
  $('#home-body').innerHTML = `
    <div class="facts">
      <div>
        <div class="k">Sealed loss rate</div>
        <div class="v">${nf(m.loss_rate, 2)}<small> / hour</small></div>
        <div class="meaning">With everything shut, half the excess particulate is gone in
          ${nf(m.sealed_half_life_min)} minutes. Leakage plus deposition, measured from
          ${m.n_quiet_intervals} quiet readings.</div>
      </div>
      <div>
        <div class="k">Sealed floor now</div>
        <div class="v">${nf(m.sealed_floor, 1)}<small> µg/m³</small></div>
        <div class="meaning">Where this flat settles when nothing is happening.</div>
      </div>
      <div>
        <div class="k">Outdoor, quiet street</div>
        <div class="v">${nf(m.outdoor_estimate, 1)}<small> µg/m³</small></div>
        <div class="meaning">Estimated from the openings, not from any outdoor sensor.
          SASH has none.</div>
      </div>
      <div>
        <div class="k">CO removal, sealed</div>
        <div class="v">${nf(m.co_sealed_loss, 2)}<small> / hour</small></div>
        <div class="meaning">The tracer channel's own decay with the home shut. It is the
          intercept of the regression that measures every exchange rate.</div>
      </div>
      <div>
        <div class="k">History</div>
        <div class="v">${nf(model.history_days, 0)}<small> days</small></div>
        <div class="meaning">${model.n_intervals.toLocaleString()} analysis intervals,
          ${m.n_open_intervals} of them with exactly one opening open.</div>
      </div>
      <div>
        <div class="k">Indoor emissions found</div>
        <div class="v">${m.n_indoor_source}</div>
        <div class="meaning">${Object.entries(model.sources.by_reason)
          .map(([k, v]) => `${v} ${k}`).join('; ') || 'none yet'}. Excluded from the window
          model, kept on the timeline.</div>
      </div>
    </div>`;
}

/* ============================ TIMELINE ============================ */

function renderTimeline(tl) {
  const W = 1000;
  const padL = 96, padR = 12, padT = 18;
  const pmH = 150, gap = 16, streetH = 46, openH = 13, openGap = 5, axisH = 20;
  const nOpen = Math.max(tl.openings.length, 1);
  const openTop = padT + pmH + gap + streetH + gap;
  const H = openTop + nOpen * (openH + openGap) + axisH;

  const t0 = tl.start, t1 = tl.end, span = Math.max(t1 - t0, 1);
  const x = t => padL + ((t - t0) / span) * (W - padL - padR);

  const pm = tl.samples.filter(s => s.pm25 !== null);
  const pmMax = Math.max(...pm.map(s => s.pm25), 20) * 1.12;
  const y = v => padT + pmH - (v / pmMax) * pmH;

  const line = pm.length
    ? `<path d="${pm.map((s, i) => `${i ? 'L' : 'M'}${x(s.t).toFixed(1)},${y(s.pm25).toFixed(1)}`).join('')}"
             fill="none" stroke="var(--air)" stroke-width="1.6" stroke-linejoin="round"/>`
    : '';

  const grid = [0, 0.25, 0.5, 0.75, 1].map(f => {
    const v = pmMax * (1 - f), yy = padT + pmH * f;
    return `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="var(--line-soft)" stroke-width="1"/>
            <text x="${padL - 8}" y="${yy + 3}" fill="var(--ink-faint)" font-size="9" text-anchor="end">${v.toFixed(0)}</text>`;
  }).join('');

  const sources = tl.indoor_source_spans.map(s =>
    `<rect x="${x(s.from)}" y="${padT}" width="${Math.max(x(s.to) - x(s.from), 1)}" height="${pmH}"
           fill="rgba(232,115,74,.14)"/>`).join('');

  const stMax = Math.max(...tl.street.map(s => s.events), 1);
  const bw = (W - padL - padR) / Math.max(tl.street.length, 1);
  const streetTop = padT + pmH + gap;
  const streetBars = tl.street.map((s, i) => {
    const bh = (s.events / stMax) * streetH;
    return `<rect x="${padL + i * bw}" y="${streetTop + streetH - bh}"
                  width="${Math.max(bw - 0.8, 0.8)}" height="${Math.max(bh, 0.4)}"
                  fill="var(--street-deep)" opacity=".9"/>`;
  }).join('');

  const openBands = tl.openings.map((o, i) => {
    const yy = openTop + i * (openH + openGap);
    const spans = o.spans.map(s =>
      `<rect x="${x(s.from)}" y="${yy}" width="${Math.max(x(s.to) - x(s.from), 2)}" height="${openH}"
             rx="2" fill="rgba(84,214,194,.40)"/>`).join('');
    return `<line x1="${padL}" y1="${yy + openH / 2}" x2="${W - padR}" y2="${yy + openH / 2}"
                  stroke="var(--line-soft)" stroke-width="1" opacity=".55"/>
      ${spans}
      <text x="${padL - 8}" y="${yy + openH - 2}" fill="var(--ink-faint)" font-size="9"
            text-anchor="end">${o.label}</text>`;
  }).join('');

  const ticks = [];
  const step = span > 4 * 86400 ? 86400 : span > 86400 ? 6 * 3600 : 3 * 3600;
  for (let t = Math.ceil(t0 / step) * step; t < t1; t += step) {
    ticks.push(`<line x1="${x(t)}" y1="${padT}" x2="${x(t)}" y2="${padT + pmH}"
        stroke="var(--line-soft)" stroke-width="1" opacity=".6"/>
      <text x="${x(t)}" y="${H - 5}" fill="var(--ink-faint)" font-size="9" text-anchor="middle">${
        step >= 86400
          ? new Date(t * 1000).toLocaleDateString('en-GB', { timeZone: 'UTC', weekday: 'short' })
          : hhmm(t)}</text>`);
  }

  $('#timeline').innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
      ${sources}${grid}${ticks.join('')}${line}${streetBars}${openBands}
      <text x="${padL - 8}" y="${streetTop + streetH}" fill="var(--ink-faint)" font-size="9"
            text-anchor="end">street</text>
      <text x="${padL - 8}" y="${padT - 6}" fill="var(--ink-faint)" font-size="9"
            text-anchor="end">µg/m³</text>
    </svg>`;

  $('#events').className = '';
  $('#events').innerHTML = tl.events.length
    ? tl.events.map(e => `<div class="event-row ${e.kind}">
        <span class="t">${dayHM(e.t)}</span><span class="txt">${e.text}</span></div>`).join('')
    : '<p class="hint" style="margin:0">Nothing in this window.</p>';
}

/* ============================ RECORD ============================ */

function renderLedger(led) {
  renderBacktest(led.backtest || {});

  $('#ledger-summary').innerHTML = `
    <div class="ledger-stats">
      <div><div class="k">Published</div><div class="v">${led.n_total}</div></div>
      <div><div class="k">Acted on and scored</div><div class="v">${led.n_verified}</div></div>
      <div><div class="k">Not acted on</div><div class="v">${led.n_not_followed}</div></div>
      <div><div class="k">Inside the interval</div><div class="v">${
        led.coverage === null ? '--' : nf(led.coverage * 100) + '%'}</div></div>
    </div>`;

  const rows = led.plans;
  $('#ledger').className = '';
  if (!rows.length) {
    $('#ledger').innerHTML = `<p class="hint" style="margin:0">No plans published in this session yet.
      Publish one from the Now tab, then press Advance until its window has passed and it will be scored
      here. A plan the household did not act on is recorded and not scored.</p>`;
    return;
  }

  const errBar = (pred, lo, hi, realised) => {
    const half = Math.max((hi - lo) / 2, 1e-6);
    const z = Math.max(-2.4, Math.min(2.4, (realised - pred) / half));
    const at = v => 50 + (v / 2.6) * 50;
    return `<div class="mini-interval">
        <div class="rng" style="left:${at(-1)}%;width:${at(1) - at(-1)}%"></div>
        <div class="real ${Math.abs(z) > 1 ? 'out' : ''}" style="left:${at(z)}%"></div>
      </div>`;
  };
  $('#ledger').innerHTML = `
    <table class="ledger">
      <thead><tr>
        <th>Published</th><th>Opening</th><th>Ran at</th>
        <th class="num">Forecast</th><th>Forecast vs realised</th>
        <th class="num">Realised</th><th>Verdict</th>
      </tr></thead>
      <tbody>${rows.map(r => {
        return `<tr>
          <td>${dayHM(r.created_at)}</td>
          <td>${r.opening_label}</td>
          <td>${dayHM(r.start_at)}</td>
          <td class="num">${nf(r.pred_intake)}</td>
          <td>${r.realized_intake === null || r.realized_intake === undefined ? ''
            : errBar(r.pred_intake, r.pred_intake_lo, r.pred_intake_hi, r.realized_intake)}</td>
          <td class="num">${r.realized_intake === null || r.realized_intake === undefined ? '--' : nf(r.realized_intake)}</td>
          <td><span class="verdict-pill ${r.verdict || r.status}">${
            (r.verdict || r.status).replace(/_/g, ' ')}</span></td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

function renderBacktest(bt) {
  const el = $('#backtest');
  el.className = '';
  if (!bt.n_scored) {
    el.innerHTML = `<p class="hint" style="margin:0">${bt.ranking_note || 'Not enough history yet.'}</p>`;
    return;
  }
  const cal = bt.calibration || {};
  const rows = bt.airings || [];
  const errBar = (pred, lo, hi, realised) => {
    const half = Math.max((hi - lo) / 2, 1e-6);
    const z = Math.max(-2.4, Math.min(2.4, (realised - pred) / half));
    const at = v => 50 + (v / 2.6) * 50;
    return `<div class="mini-interval">
        <div class="rng" style="left:${at(-1)}%;width:${at(1) - at(-1)}%"></div>
        <div class="real ${Math.abs(z) > 1 ? 'out' : ''}" style="left:${at(z)}%"></div>
      </div>`;
  };

  el.innerHTML = `
    <div class="ledger-stats">
      <div><div class="k">Airings priced blind</div><div class="v">${bt.n_scored}</div></div>
      <div><div class="k">Inside the interval</div><div class="v">${nf(bt.coverage * 100)}<small>% of a 90% interval</small></div></div>
      <div><div class="k">Median error</div><div class="v">${nf(bt.median_abs_error)}</div></div>
      <div><div class="k">Median airing</div><div class="v">${nf(bt.median_realised)}</div></div>
    </div>
    <p class="hint">
      The record was cut in three. The model was fitted on the first ${nf(bt.fit_days)} days and never saw the rest.
      The interval was sized on the middle slice, from ${cal.n} airings, at ${nf(cal.k, 1)} times the parameter
      bootstrap. Then every airing in the last ${nf(bt.held_days)} days was forecast before it happened and compared
      with what the readings afterwards showed it cost.
      ${bt.ranking_held ? `<span class="ok-note">${bt.ranking_note}.</span>`
                        : `<span class="bad-note">${bt.ranking_note}.</span>`}
    </p>
    <table class="ledger">
      <thead><tr><th>Airing</th><th>Opening</th><th class="num">Minutes</th>
        <th class="num">Forecast</th><th>Where it landed in its interval</th><th class="num">Realised</th><th>Verdict</th></tr></thead>
      <tbody>${rows.slice(0, 12).map(a => `
        <tr>
          <td>${dayHM(a.start_at)}</td>
          <td>${a.opening_label}</td>
          <td class="num">${nf(a.duration_min)}</td>
          <td class="num">${nf(a.pred_intake)}</td>
          <td>${errBar(a.pred_intake, a.pred_intake_lo, a.pred_intake_hi, a.realised_intake)}</td>
          <td class="num">${nf(a.realised_intake)}</td>
          <td><span class="verdict-pill ${a.verdict}">${a.verdict.replace(/_/g, ' ')}</span></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

/* ============================ SETUP ============================ */

function renderHealth(h) {
  $('#health').className = '';
  $('#health').innerHTML = h.checks.map(c => `
    <div class="check ${c.ok ? 'ok' : 'bad'}">
      <span class="dot"></span>
      <span><span class="name">${c.name}</span><br><span class="detail">${c.detail}</span></span>
    </div>`).join('');
}

const ROLES = [
  ['opening', 'An opening (window or door)'],
  ['street_camera', 'The camera watching the street'],
  ['indoor_air', 'The indoor Air Quality Monitor'],
  ['unused', 'Not used by SASH'],
];

function renderDevices(d) {
  $('#devices').className = '';
  $('#devices').innerHTML = `
    <p class="hint" style="margin-top:0">Linked to <code>${d.linked_to}</code>.</p>
    <table class="devices">
      <thead><tr><th>Ring name</th><th>Device family</th><th>Means, in this home</th>
        <th>Side it faces</th><th>Ring device id</th><th>State</th></tr></thead>
      <tbody>${d.devices.map(x => `
        <tr data-device="${x.device_id}">
          <td>${x.ring_name}</td>
          <td><code>${x.family}</code></td>
          <td><select class="role" data-device="${x.device_id}">${
            ROLES.map(([v, t]) => `<option value="${v}" ${v === x.app_role ? 'selected' : ''}>${t}</option>`).join('')
          }</select></td>
          <td>${x.app_role === 'opening'
            ? `<select class="orient" data-device="${x.device_id}">${
                ['street', 'courtyard', 'garden', 'unknown'].map(v =>
                  `<option value="${v}" ${v === x.orientation ? 'selected' : ''}>${v}</option>`).join('')}</select>`
            : `<span style="color:var(--ink-faint)">--</span>`}</td>
          <td class="id">${x.device_id}</td>
          <td>${x.online ? 'online' : '<span style="color:var(--warn)">offline</span>'}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;

  const save = async (deviceId) => {
    const row = $(`tr[data-device="${deviceId}"]`);
    const role = $('select.role', row).value;
    const orient = $('select.orient', row);
    const label = row.children[0].textContent.trim();
    await fetch(`/api/devices/${deviceId}/role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app_role: role, label, orientation: orient ? orient.value : null }),
    });
    toast('Mapping saved. Everything downstream is being recomputed.');
    refresh();
  };
  $$('#devices select').forEach(sel => sel.onchange = () => save(sel.dataset.device));
}

/* ============================ wiring ============================ */

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const [status, rec] = await Promise.all([get('/api/status'), get('/api/recommendation')]);
    renderLive(status);
    renderDecision(rec);
    renderDemand(rec, status);
    renderAlternatives(rec);

    renderTimeline(await get(`/api/timeline?hours=${state.hours}`));
    renderStreetProfile(await get('/api/activity'));

    const model = await get('/api/model');
    renderOpenings(model);
    renderVerdicts(model);
    renderHome(model);

    renderLedger(await get('/api/ledger'));
    renderHealth(await get('/api/health'));
    renderDevices(await get('/api/devices'));
  } catch (e) {
    console.error(e);
    toast('Could not reach SASH: ' + e.message);
  } finally {
    state.busy = false;
  }
}

$$('#tabs button').forEach(b => b.onclick = () => {
  $$('#tabs button').forEach(x => x.classList.toggle('active', x === b));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + b.dataset.view));
  state.view = b.dataset.view;
});

$$('#timeline-range button').forEach(b => b.onclick = async () => {
  $$('#timeline-range button').forEach(x => x.classList.toggle('active', x === b));
  state.hours = Number(b.dataset.hours);
  renderTimeline(await get(`/api/timeline?hours=${state.hours}`));
});

const advanceBtn = $('#advance');
const advanceLongBtn = $('#advance-long');

/* Four hours exists because a published plan cannot be scored until its window
 * and the ninety-minute recovery after it have both passed. Thirty minutes is
 * for watching events arrive; four hours is for letting a plan run its course. */
function wireAdvance(button, minutes, label) {
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = 'Advancing…';
    try {
      const r = await post(`/api/demo/advance?minutes=${minutes}`);
      toast(r.ok
        ? `${r.webhooks_delivered} Ring webhooks delivered, ${r.aq_polls} monitor readings fetched.`
        : r.reason || 'The simulated tape has run out.');
      await refresh();
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  };
}
wireAdvance(advanceBtn, 30, 'Advance 30 min');
wireAdvance(advanceLongBtn, 240, 'Advance 4 h');

get('/healthz')
  .then(h => {
    advanceBtn.hidden = advanceLongBtn.hidden = !h.demo_controls;
    state.canFollow = !!h.demo_follow;
  })
  .catch(() => { advanceBtn.hidden = advanceLongBtn.hidden = true; })
  .finally(refresh);
