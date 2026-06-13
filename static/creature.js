/* Creature compositor — layers a procedural ASCII creature from the
 * creature_spec payload (/api/status) at 10fps.
 *
 * Layers, stamped bottom-up onto a char matrix each frame:
 *   body (breath phase) -> appendages (own sway periods) -> eyes (blink/
 *   saccade state machines) -> mouth (expression / speak cycle) -> fx glyphs.
 * Trembling and tints are CSS classes on #creature-art, not grid shifts.
 *
 * Inputs merged per channel, freshest wins:
 *   applyStatus(spec)  — /api/status every ~2.5s (morphology + expr truth)
 *   applyActivity(a)   — /api/activity 500ms-3s (thinking/executing/dreaming…)
 *   setSpeaking(bool)  — #nx-audio media events (instant)
 */
const Creature = (() => {
    let spec = null;
    let activity = { state: '' };
    let speaking = false;
    let lastDrawn = '';
    let masterTimer = null;

    // animation state
    const eyes = [{ nextBlink: 0, blinkUntil: 0 }, { nextBlink: 0, blinkUntil: 0 }];
    let nextSaccade = 0, saccadeUntil = 0, saccadeDir = 0;
    let nextMicro = 0, microUntil = 0, microKind = null, microStart = 0;
    let recoveryUntil = 0, smileUntil = 0, lastCondition = '';
    let nextTremble = 0, trembleUntil = 0;
    let bannerTimer = null;

    // delegate mini-me — a 1:1 render of a running delegate job at the bench
    // (terrarium under-row cells 15-18). null when no delegate is working.
    let mini = null;
    const miniSeen = {};            // name -> last status, so a finished-on-load never replays
    const MINI_SPRITE = { dot: '(o)', ring: '(O)', glow: '(°)', slit: '(=)', star: '(*)' };

    // pending-proposal tablet — eiDOS is asking Dean to approve something. Held up
    // ("presenting") until the page has been visible ~60s since it appeared, then set
    // down beside the creature (still visible = still pending; the beg is removed).
    let pendingOn = false, tabletVisibleMs = 0, tabletDown = false;
    // consume / approval beats — edge events the creature answers within a frame.
    let beatHigh = parseInt(localStorage.getItem('eidosBeatSeq') || '0', 10) || 0;
    let beatUntil = 0, beatKind = '';
    const TABLET_PRESENT_MS = 60000;
    let bondTier = 0;
    let greetedThisLoad = false;    // greeting fires at most once per page load

    const now = () => Date.now();
    const rand = (a, b) => a + Math.random() * (b - a);

    function el() { return document.getElementById('creature-art'); }

    function frameRows(rows) {
        // Wrap the w-wide creature in the 23-col terrarium frame (Phase C): one sky
        // row above, the body centered, ground + under rows below. No terrarium in
        // the payload (older server / legacy) → just the bare creature, unchanged.
        const terr = spec && spec.terrarium;
        if (!terr) return rows.map(r => r.join(''));
        const fw = terr.frame_w;
        // B3 (bond ≥ "settles near you"): resting position drifts toward the
        // mailbox (left) edge — 1 char at B3, 2 at B5.
        const drift = bondTier >= 5 ? 2 : bondTier >= 3 ? 1 : 0;
        const off = Math.max(0, Math.floor((fw - spec.w) / 2) - drift);
        const pad = (s) => (' '.repeat(off) + s + ' '.repeat(fw)).slice(0, fw);
        const under = terr.under.split('');
        stampMini(under);
        return [terr.sky, ...rows.map(r => pad(r.join(''))), terr.ground, under.join('')];
    }

    function _place(arr, c, s) {
        for (let i = 0; i < s.length; i++) if (c + i < arr.length) arr[c + i] = s[i];
    }

    function stampMini(under) {
        // The mini-me lives on the reserved bench (under-row cells 15-18): 3-char
        // sprite + a status glyph. Every glyph is the delegate row's true state.
        if (!mini) return;
        const t = now();
        const sprite = MINI_SPRITE[(spec.eyes && spec.eyes.family)] || '(o)';
        if (mini.phase === 'working') {
            _place(under, 15, sprite);
            under[18] = mini.mode === 'code'
                ? ['#', '|', '/', '-', '\\'][Math.floor(t / 300) % 5]
                : (Math.floor(t / 500) % 2 ? '?' : '·');
        } else if (mini.phase === 'return') {          // success — carries a spark, then merges
            _place(under, 15, sprite); under[18] = '✦';
            if (t - mini.since > 1500) mini = null;
        } else if (mini.phase === 'fail') {            // empty-handed, no lingering shame
            _place(under, 15, sprite); under[18] = '~';
            if (t - mini.since > 1500) mini = null;
        } else if (mini.phase === 'die') {             // the watchdog killed it at the bench
            _place(under, 15, '(x)');
            if (t - mini.since > 2000) mini = null;
        }
    }

    function applyBeats(beats) {
        let maxSeq = beatHigh, latest = null;
        for (const b of (beats || [])) {
            const n = parseInt(String(b.id || '').slice(1), 10);
            if (!isNaN(n) && n > maxSeq) { maxSeq = n; latest = b; }
        }
        if (latest) {                       // only the newest beat plays; never replays
            beatHigh = maxSeq;
            localStorage.setItem('eidosBeatSeq', String(beatHigh));
            beatKind = latest.type; beatUntil = now() + 1500;
        }
    }

    function applyPending(p) {
        const on = !!(p && (p.self_guide || (p.selfedits || 0) > 0));
        if (on && !pendingOn) { tabletVisibleMs = 0; tabletDown = false; }
        pendingOn = on;
    }

    function applyBond(s) {
        bondTier = (s.bond_expr && s.bond_expr.tier) || 0;
        // B2 greeting ("it saw me"): on a fresh load after a >=4h gap, a bonded
        // creature brightens and smiles once. lastSeen marks "the page was alive";
        // a quick tab-switch (recent lastSeen) earns nothing.
        const last = parseFloat(localStorage.getItem('eidosLastSeen') || '0');
        const t = now();
        if (bondTier >= 2 && !greetedThisLoad && last && (t - last) > 4 * 3600 * 1000) {
            smileUntil = t + 1800;          // reuse the smile channel: bright eyes + smile
            greetedThisLoad = true;
        }
        localStorage.setItem('eidosLastSeen', String(t));
        // hover line — a diegetic ledger, never a meter
        const box = document.getElementById('creature-box');
        const h = s.bond_hover;
        if (box && h) {
            const parts = [];
            if (h.exchanges) parts.push(h.exchanges + ' exchange' + (h.exchanges > 1 ? 's' : ''));
            if (h.hold_min) parts.push('sat with you ' +
                (h.hold_min >= 60 ? (h.hold_min / 60).toFixed(1) + 'h' : h.hold_min + 'm'));
            if (h.approvals) parts.push(h.approvals + ' idea' + (h.approvals > 1 ? 's' : '') + ' approved');
            box.title = parts.join(' · ');
        }
    }

    function stampBeatTablet(rows, t) {
        // consume beat: a · mote drifts in from the mailbox side toward the body
        if (t < beatUntil && beatKind === 'consume') {
            const p = 1 - (beatUntil - t) / 1500;
            const col = Math.max(0, Math.min(spec.w - 1, Math.floor(1 + p * (spec.w - 2))));
            stamp(rows, Math.max(0, spec.h - 2), col, '·');
        }
        // pending-proposal tablet: held up, then set down beside the creature after
        // ~60s of visible page time. Present = still pending; gone = decided.
        if (pendingOn && spec.eyes) {
            if (!tabletDown) {
                if (document.visibilityState !== 'hidden') tabletVisibleMs += 100;
                if (tabletVisibleMs >= TABLET_PRESENT_MS) tabletDown = true;
            }
            stamp(rows, tabletDown ? spec.h - 1 : spec.eyes.l[0], spec.w - 1, '▽');
        }
    }

    function updateMini(dels) {
        dels = dels || [];
        const byName = {};
        for (const d of dels) byName[d.name] = d;
        // an active mini whose job left "running" plays its return/fail/die/vanish
        if (mini && mini.phase === 'working') {
            const d = byName[mini.name];
            if (!d || d.status !== 'running') {
                const st = d ? d.status : 'reaped';
                if (st === 'completed') { mini.phase = 'return'; mini.since = now(); }
                else if (st === 'failed') { mini.phase = 'fail'; mini.since = now(); }
                else if (st === 'timed_out') { mini.phase = 'die'; mini.since = now(); }
                else { mini = null; }                  // reaped → vanish instantly (a restart killed it)
            }
        }
        // adopt a newly-running delegate (only when the bench is free). A delegate already
        // finished on first page load is marked seen below and never replays.
        if (!mini) {
            for (const d of dels) {
                if (d.status === 'running' && miniSeen[d.name] !== 'running') {
                    mini = { name: d.name, mode: d.mode || 'research', phase: 'working',
                             since: now() }; break;
                }
            }
        }
        for (const d of dels) miniSeen[d.name] = d.status;
    }

    /* ---------------- expression resolution ---------------- */

    function effectiveState() {
        const x = (spec && spec.expr) || {};
        const act = activity.state || '';
        if (x.dead) return 'dead';
        if (act === 'dreaming') return 'dreaming';
        // A running delegate is a BACKGROUND job, not the creature's own state — it must
        // NOT mask thinking/executing/strained (that was a rendered falsehood). Phase C
        // shows it as a separate mini-me sprite; until then a delegate isn't a mood.
        const cond = x.condition || 'STABLE';
        if (cond === 'STRAINED') return 'strained';
        if (cond === 'RUMINATING') return 'ruminating';
        if (cond === 'RECOVERY' || now() < recoveryUntil) return 'recovery';
        if (act === 'thinking') return 'thinking';
        if (act === 'executing') return 'executing';
        if (act === 'sleeping' || !x.has_goal || x.paused) return 'sleeping';
        return 'idle';
    }

    function listeningOverlay(state) {
        const x = (spec && spec.expr) || {};
        const held = x.listening || activity.state === 'listening';
        return held && state !== 'dead' && state !== 'dreaming';
    }

    /* ---------------- layer stamps ---------------- */

    function stamp(rows, r, c, text) {
        if (r < 0 || r >= rows.length) return;
        for (let i = 0; i < text.length; i++) {
            const cc = c + i;
            if (cc >= 0 && cc < rows[r].length) rows[r][cc] = text[i];
        }
    }

    function stampAppendages(rows, t) {
        const amp = Math.max(0, Math.min(2, (spec.anim && spec.anim.sway_amp) || 0));
        for (const ap of (spec.appendages || [])) {
            const nFrames = Math.min(ap.frames.length, amp + 1);
            const idx = nFrames <= 1 ? 0 : Math.floor(t / ap.period_ms) % nFrames;
            const frame = ap.frames[idx];
            ap.cells.forEach((cell, i) => stamp(rows, cell[0], cell[1], frame[i] || ' '));
        }
    }

    function eyeGlyph(state, side, t) {
        // Rest pupils mirror around center: left eye at socket cell 1, right at
        // cell 0. Saccades shift BOTH pupils to cell 0 (left) or cell 1 (right).
        const g = spec.eyes.glyphs;
        const rest = side === 0 ? 1 : 0;
        if (state === 'dead') return { glyph: g.dead, pos: rest };
        if (state === 'sleeping' || state === 'dreaming') return { glyph: g.closed, pos: rest };
        if (now() < eyes[side].blinkUntil) return { glyph: g.closed, pos: rest };
        if (state === 'strained') return { glyph: g.strain, pos: rest };
        if (now() < smileUntil || (microKind === 'stretch' && now() < microUntil))
            return { glyph: g.happy, pos: rest };
        if (state === 'ruminating') {  // slow sideways wander
            const left = Math.floor(t / 1400) % 2 === 0;
            return { glyph: left ? g.look_l : g.look_r, pos: left ? 0 : 1 };
        }
        if (saccadeDir !== 0 && now() < saccadeUntil) {
            return saccadeDir < 0 ? { glyph: g.look_l, pos: 0 } : { glyph: g.look_r, pos: 1 };
        }
        return { glyph: g.open, pos: rest };
    }

    function mouthText(state, t) {
        const m = spec.mouth.glyphs;
        if (speaking && state !== 'dead' && state !== 'sleeping') {
            return m.speak[Math.floor(t / 120) % m.speak.length];
        }
        if (state === 'dead') return m.frown;
        if (state === 'sleeping' || state === 'dreaming') return m.sleep;
        if (state === 'strained') return m.grit;
        if (state === 'ruminating') return m.frown;
        if (microKind === 'groom' && now() < microUntil)
            return m.speak[Math.floor(t / 300) % m.speak.length];
        if (now() < smileUntil) return m.smile;
        return m.idle;
    }

    function stampFx(rows, state, t) {
        const w = spec.w;
        const step = Math.floor(t / 400);
        if (state === 'strained') {
            stamp(rows, 0, w - 3, step % 2 ? "'" : '°');
            stamp(rows, 1, 1, step % 2 ? '°' : "'");
        } else if (state === 'ruminating') {
            const cyc = ['·', 'o', '°', '@'];
            stamp(rows, 1, w - 3, cyc[step % 4]);
            stamp(rows, 0, w - 2, cyc[(step + 1) % 4]);
        } else if (state === 'sleeping' || state === 'dreaming') {
            const f = state === 'dreaming' ? '✦' : 'z';
            const F = state === 'dreaming' ? '·' : 'Z';
            stamp(rows, 1, w - 4, step % 2 ? f : ' ');
            stamp(rows, 0, w - 3, step % 2 ? ' ' : F);
        } else if (state === 'thinking') {
            stamp(rows, 0, Math.floor(w / 2) - 1, '...'.slice(0, 1 + (step % 3)));
        } else if (state === 'recovery' && now() < recoveryUntil) {
            stamp(rows, spec.h - 1, 0, '·');
            stamp(rows, spec.h - 2, w - 1, '·');
        }
    }

    /* ---------------- timers / micro-behaviors ---------------- */

    function scheduleTimers(t) {
        const a = spec.anim || {};
        for (let i = 0; i < 2; i++) {
            if (t >= eyes[i].nextBlink) {
                eyes[i].blinkUntil = t + rand(100, 220);
                const dbl = Math.random() < 0.15;
                eyes[i].nextBlink = t + (a.blink_ms || 3200)
                    + rand(-(a.blink_jitter_ms || 1500), (a.blink_jitter_ms || 1500))
                    + (i ? 80 : -80) + (dbl ? -((a.blink_ms || 3200) * 0.85) : 0);
            }
        }
        if (t >= nextSaccade) {
            // Supervisor glance: while a mini-me works the bench (right side), the
            // creature sometimes checks on its delegate instead of a random saccade.
            saccadeDir = (mini && mini.phase === 'working' && Math.random() < 0.35)
                ? 1 : (Math.random() < 0.5 ? -1 : 1);
            saccadeUntil = t + rand(600, 900);
            nextSaccade = t + (a.saccade_ms || 5500) * rand(0.6, 1.4);
        }
        if (t >= nextMicro) {
            microKind = ['stretch', 'look_around', 'groom'][Math.floor(Math.random() * 3)];
            microStart = t;
            microUntil = t + rand(1500, 3000);
            nextMicro = t + (a.micro_ms || 12000) * rand(0.7, 1.3);
        }
    }

    /* ---------------- main render ---------------- */

    function render() {
        if (!spec || !spec.base || !el()) return;
        const t = now();

        // Metamorphosis interlude: the body is a chrysalis — slow pulse, soft
        // shimmer, every other behavior suspended until emergence.
        if (spec.interlude) {
            const phase = Math.floor(t / 1600) % 2;
            const rows = spec.base[phase].map(line => line.split(''));
            const shim = Math.floor(t / 700) % 3;
            if (shim !== 2) stamp(rows, 1, shim ? 1 : spec.w - 2, '✦');
            const text = frameRows(rows).join('\n');
            if (text !== lastDrawn) { el().textContent = text; lastDrawn = text; }
            el().style.color = spec.accent || '';
            el().classList.remove('cr-tremble', 'cr-dead', 'cr-listening');
            el().classList.add('cr-dreaming');   // chrysalis glow rides the dream tint
            return;
        }
        el().classList.remove('cr-dreaming');

        const state = effectiveState();
        const a = spec.anim || {};

        // breath phase (state-scaled), each phase held >= 1s
        let breath = a.breath_ms || 3400;
        if (state === 'dreaming' || state === 'sleeping') breath *= 1.8;
        if (state === 'executing') breath *= 0.8;
        const phase = Math.floor(t / Math.max(1000, breath / 2)) % 2;
        let rows = spec.base[phase].map(line => line.split(''));

        const idleish = state === 'idle' || state === 'executing' || state === 'thinking';
        if (!idleish) { microKind = null; microUntil = 0; }

        if (state !== 'dead') {
            scheduleTimers(t);
            stampAppendages(rows, state === 'dead' ? 0 : t);
        } else {
            stampAppendages(rows, 0);   // frozen pose
        }

        // consume beat overrides the gaze toward the mailbox ("it read me")
        if (t < beatUntil && beatKind === 'consume' && state !== 'dead') {
            saccadeDir = -1; saccadeUntil = beatUntil;
        }
        // B1 (bond ≥ "notices you"): the gaze parks on the mailbox while it's
        // actively listening to Dean — sustained attention, not a random glance.
        if (bondTier >= 1 && state !== 'dead' && listeningOverlay(state)) {
            saccadeDir = -1; saccadeUntil = t + 300;
        }

        if (spec.eyes) {
            // micro: look_around chain overrides saccade direction
            if (microKind === 'look_around' && t < microUntil) {
                const ph = (t - microStart) / (microUntil - microStart);
                saccadeDir = ph < 0.33 ? -1 : ph < 0.66 ? 1 : 0;
                saccadeUntil = t + 100;
            }
            for (const side of [0, 1]) {
                const e = side === 0 ? spec.eyes.l : spec.eyes.r;
                const closed = (microKind === 'groom' && t < microUntil);
                const res = closed ? { glyph: spec.eyes.glyphs.closed, pos: 0 }
                                   : eyeGlyph(state, side, t);
                if (listeningOverlay(state) && res.glyph === spec.eyes.glyphs.open) {
                    res.glyph = spec.eyes.glyphs.open === 'o' ? 'O' : res.glyph;
                }
                stamp(rows, e[0], e[1], ' ');       // clear both socket cells
                stamp(rows, e[0], e[1] + 1, ' ');
                stamp(rows, e[0], e[1] + res.pos, res.glyph);
            }
        }
        if (spec.mouth) {
            stamp(rows, spec.mouth.row, spec.mouth.col, mouthText(state, t));
        }
        stampFx(rows, state, t);
        stampBeatTablet(rows, t);

        // micro stretch: lift the body one row
        if (microKind === 'stretch' && t < microUntil && rows.length > 1) {
            rows = rows.slice(1).concat([rows[0].map(() => ' ')]);
        }

        const text = frameRows(rows).join('\n');
        if (text !== lastDrawn) { el().textContent = text; lastDrawn = text; }

        // STRAINED trembles in intermittent ~2-3s bursts every ~20s, not continuously
        // (the steady sweat drop in stampFx is the always-on tell). Onset bursts at once.
        let tremble = false;
        if (state === 'strained') {
            if (t >= nextTremble) {
                trembleUntil = t + rand(2000, 3000);
                nextTremble = t + rand(18000, 24000);
            }
            tremble = t < trembleUntil;
        } else {
            nextTremble = 0; trembleUntil = 0;
        }

        const pre = el();
        pre.style.color = spec.accent || '';
        pre.classList.toggle('cr-tremble', tremble);
        pre.classList.toggle('cr-dead', state === 'dead');
        pre.classList.toggle('cr-dreaming', state === 'dreaming');
        pre.classList.toggle('cr-listening', listeningOverlay(state));
    }

    /* ---------------- events banner ---------------- */

    // FROZEN at these 4 milestone beats — anti-annoyance rule: no new banner clients.
    const EVENT_TEXT = {
        laid: 'a new egg appears…', crack: 'the egg is cracking!',
        hatched: '✦ the egg hatched! ✦', metamorphosis: '✦ metamorphosis! ✦',
    };

    function checkEvents(s) {
        const evts = s.events || [];
        if (!evts.length) return;
        const seen = parseFloat(localStorage.getItem('eidosCreatureEvt') || '0');
        const fresh = evts.filter(e => e.ts > seen);
        if (!fresh.length) return;
        localStorage.setItem('eidosCreatureEvt', String(evts[evts.length - 1].ts));
        const msg = EVENT_TEXT[fresh[fresh.length - 1].kind];
        if (!msg) return;
        let b = document.getElementById('creature-banner');
        if (!b) {
            b = document.createElement('div');
            b.id = 'creature-banner';
            const info = document.querySelector('.creature-info');
            if (info && info.parentNode) info.parentNode.insertBefore(b, info);
            else return;
        }
        b.textContent = msg;
        b.classList.add('show');
        clearTimeout(bannerTimer);
        bannerTimer = setTimeout(() => b.classList.remove('show'), 5000);
    }

    /* ---------------- public API ---------------- */

    return {
        applyStatus(s) {
            const fresh = !spec || spec.id !== s.id || spec.stage !== s.stage;
            const prevCond = spec && spec.expr ? spec.expr.condition : '';
            const emerged = spec && spec.interlude && !s.interlude;
            spec = s;
            if (fresh) { lastDrawn = ''; }
            if (emerged) {                       // metamorphosis complete — flash
                const pre = el();
                if (pre) {
                    pre.classList.add('cr-emerge');
                    setTimeout(() => pre.classList.remove('cr-emerge'), 2600);
                }
            }
            if (prevCond !== 'RECOVERY' && s.expr && s.expr.condition === 'RECOVERY'
                    && lastCondition !== 'RECOVERY') {
                recoveryUntil = now() + 3000;
                smileUntil = now() + 8000;
            }
            lastCondition = (s.expr && s.expr.condition) || '';
            updateMini(s.delegates);
            applyPending(s.pending);
            applyBeats(s.beats);
            applyBond(s);
            checkEvents(s);
            if (!masterTimer) masterTimer = setInterval(render, 100);
            render();
        },
        applyActivity(a) { activity = a || { state: '' }; },
        setSpeaking(b) { speaking = !!b; },
        // genome-matched mini sprite (workbench crew renders the pet's agents with it)
        miniSprite() { return MINI_SPRITE[(spec && spec.eyes && spec.eyes.family)] || '(o)'; },
        stop() { if (masterTimer) { clearInterval(masterTimer); masterTimer = null; } },
    };
})();

// A top-level `const` is NOT a property of window, so `window.Creature` was undefined and
// dashboard.html's `if (window.Creature)` guards silently fell back to the legacy ascii_art
// sprite — the genome compositor never rendered in the browser. Expose it explicitly.
window.Creature = Creature;
