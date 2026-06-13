/* Workbench tab — the pi mini-IDE embedded in the :8099 dashboard.
 *
 * Talks directly to the EidosCodeIDE service (:8100, CORS-enabled) the same way
 * the page already streams voice from :8098: base derived from the page host, so
 * it works over localhost AND Tailscale. window.NX_IDE_BASE is set by the inline
 * template script (dashboard.py fills {{IDE_PORT}}).
 *
 * Presentation doctrine ("literal AI"): the stints ARE real pi agents running on
 * eiDOS's infrastructure. The pet's creature-box is REPARENTED to the workbench
 * dock (same DOM node — the one creature walks over, its real state intact), and
 * each stint renders as a genome-matched crew mini on the bench whose glyph is
 * that stint's true state: spinner = its pi turn is executing now, · = running
 * idle, z = cold on disk (resumable). Pi's words appear as the crew speaking.
 */
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const BASE = window.NX_IDE_BASE || (location.protocol + '//' + location.hostname + ':8100');

    let cur = null, es = null, turnActive = false, curText = null, pendWrite = null;
    let tabs = [], active = null;
    let stints = [];                  // last /api/stints payload (rail + crew truth)
    let railTimer = null, crewTimer = null;
    let petHome = null;               // where creature-box lives on the station tab

    // Stand-by: while you're working the bench (or the crew is mid-turn), eiDOS holds
    // its autonomous house loop and attends — the SAME "listening hold" the chat box uses
    // (window.setChatHold). It lapses after WB_IDLE_MS of no interaction with no crew
    // working, so the buddy drifts back to house things only once the bench goes quiet.
    const WB_IDLE_MS = 120000;
    let wbIdleTimer = null;
    function standByTouch() {
        if (!wbOn()) return;
        if (window.setChatHold) window.setChatHold(true);   // engage / refresh (self-keeps 20s)
        if (wbIdleTimer) clearTimeout(wbIdleTimer);
        wbIdleTimer = setTimeout(standByRelease, WB_IDLE_MS);
    }
    function standByRelease() {
        if (wbIdleTimer) { clearTimeout(wbIdleTimer); wbIdleTimer = null; }
        if (window.setChatHold) window.setChatHold(false);  // buddy resumes house things
    }

    /* ---------------- tab switching ---------------- */

    function wbOn() { return document.body.classList.contains('wb'); }

    function wbTab(on) {
        document.body.classList.toggle('wb', on);
        const st = $('tab-st'), wb = $('tab-wb');
        if (st) st.classList.toggle('on', !on);
        if (wb) wb.classList.toggle('on', on);
        localStorage.setItem('eidosTab', on ? 'wb' : 'st');
        const box = $('creature-box');
        if (box) {
            if (on) {
                petHome = petHome || box.parentElement;
                const dock = $('wb-pet');
                if (dock) dock.appendChild(box);
            } else if (petHome) {
                petHome.appendChild(box);
            }
        }
        if (on) {
            listStints();
            if (!railTimer) railTimer = setInterval(listStints, 5000);
            if (!crewTimer) crewTimer = setInterval(crewRender, 250);
            standByTouch();                       // arriving = present
        } else {
            if (railTimer) { clearInterval(railTimer); railTimer = null; }
            if (crewTimer) { clearInterval(crewTimer); crewTimer = null; }
            standByRelease();                     // leaving the bench frees the buddy
        }
    }
    window.wbTab = wbTab;

    /* ---------------- crew bench (one mini per stint) ---------------- */

    function crewRender() {
        const pre = $('wb-crew');
        if (!pre) return;
        const sp = (window.Creature && Creature.miniSprite) ? Creature.miniSprite() : '(o)';
        const t = Date.now();
        let line = '', tip = [];
        for (const s of stints) {
            let g;
            if (s.status === 'running' && s.turn_active) g = ['#', '|', '/', '-', '\\'][Math.floor(t / 300) % 5];
            else if (s.status === 'running') g = '·';
            else g = 'z';
            line += (s.id === cur ? ' ▸' : '  ') + sp + g;
            tip.push(s.title + ': ' + (s.turn_active ? 'working' : s.status));
        }
        pre.textContent = line || '  (bench empty)';
        pre.title = tip.join(' · ');
        const busy = stints.some(s => s.turn_active);
        const wb = $('tab-wb');
        if (wb) wb.textContent = busy ? 'workbench ⚒' : 'workbench';
        if (busy) standByTouch();   // crew mid-turn → buddy stays present even if you're idle
    }

    /* ---------------- stints rail ---------------- */

    function add(cls, txt) {
        const log = $('wb-log');
        const d = document.createElement('div');
        d.className = cls; d.textContent = txt;
        log.appendChild(d); log.scrollTop = log.scrollHeight;
        return d;
    }

    async function listStints() {
        try {
            const j = await (await fetch(BASE + '/api/stints')).json();
            stints = j.stints || [];
        } catch (e) {
            const box = $('wb-stints');
            if (box) box.innerHTML = '<div class="sys">IDE service unreachable (:' +
                BASE.split(':').pop() + ')</div>';
            return;
        }
        const box = $('wb-stints');
        if (!box) return;
        box.innerHTML = '';
        stints.forEach(s => {
            const d = document.createElement('div');
            d.className = 'stint' + (s.id === cur ? ' active' : '');
            d.textContent = (s.status === 'running' ? '● ' : '○ ') + s.title;
            d.onclick = () => open(s.id, s.title, s.status);
            box.appendChild(d);
        });
        crewRender();
    }

    async function newStint() {
        const t = $('wb-newtitle').value || '';
        const j = await (await fetch(BASE + '/api/stints', {
            method: 'POST', body: JSON.stringify({ title: t }),
        })).json();
        if (j.id) { $('wb-newtitle').value = ''; await listStints(); open(j.id, t || j.id); }
        else add('sys', 'could not create stint: ' + (j.error || '?'));
    }
    window.wbNewStint = newStint;

    async function open(id, title, status) {
        cur = id; curText = null; turnActive = false;
        $('wb-log').innerHTML = '';
        tabs = []; active = null; renderTabs(); $('wb-viewer').textContent = '';
        $('wb-send').disabled = false; $('wb-dl').disabled = false;
        $('wb-curname').textContent = '· ' + (title || id);
        if (status && status !== 'running') {
            add('sys', 'waking the crew…');
            await fetch(BASE + '/api/stints/' + id + '/resume', { method: 'POST' });
        }
        listStints(); loadTree();
        if (es) es.close();
        es = new EventSource(BASE + '/api/stints/' + id + '/events');
        es.onmessage = e => handle(JSON.parse(e.data));
    }

    /* ---------------- pi event stream → crew chat ---------------- */

    function sprite() {
        return (window.Creature && Creature.miniSprite) ? Creature.miniSprite() : '(o)';
    }

    function handle(ev) {
        const t = ev.type;
        if (t === 'user_prompt') {
            add('msg-user', 'you ▸ ' + ev.message);
            curText = null; turnActive = true; $('wb-send').disabled = true;
        } else if (t === 'message_update' && ev.assistantMessageEvent) {
            const a = ev.assistantMessageEvent;
            if (a.type === 'text_delta') {
                if (!curText) curText = add('msg-pi', sprite() + ' ▸ ');
                curText.textContent += a.delta || '';
                $('wb-log').scrollTop = $('wb-log').scrollHeight;
            }
        } else if (t === 'tool_execution_start') {
            const ar = ev.args || {};
            const d = ar.path || ar.command || ar.pattern || '';
            add('tool', '⚙ ' + ev.toolName + (d ? ' ' + String(d).slice(0, 80) : ''));
            curText = null;
            if ((ev.toolName === 'write' || ev.toolName === 'edit') && ar.path) pendWrite = ar.path;
        } else if (t === 'tool_execution_end') {
            if (ev.isError) add('tool', '  ✗ error');
            else if (pendWrite) { loadTree(); openFile(pendWrite); pendWrite = null; }
        } else if (t === 'agent_end') {
            turnActive = false; $('wb-send').disabled = false; curText = null;
            listStints();
        } else if (t === 'stint_exit') {
            add('sys', '— crew member went to sleep —');
            $('wb-send').disabled = true;
            listStints();
        }
    }

    async function send() {
        const m = $('wb-inp').value.trim();
        if (!m || !cur || turnActive) return;
        $('wb-inp').value = '';
        const j = await (await fetch(BASE + '/api/stints/' + cur + '/prompt', {
            method: 'POST', body: JSON.stringify({ message: m }),
        })).json();
        if (!j.ok) add('sys', '✗ ' + (j.error || 'send failed'));
        else listStints();
    }
    window.wbSend = send;

    /* ---------------- code surfaces: tree · tabs · viewer · zip ---------------- */

    async function loadTree() {
        const tr = $('wb-tree');
        if (!cur) { tr.innerHTML = ''; return; }
        tr.innerHTML = '';
        await renderDir(tr, '');
    }

    async function renderDir(parent, path) {
        const j = await (await fetch(BASE + '/api/stints/' + cur + '/tree?path=' +
            encodeURIComponent(path))).json();
        (j.items || []).forEach(it => {
            const r = document.createElement('div');
            r.className = 'row ' + it.type;
            r.textContent = (it.type === 'dir' ? '▸ ' : '  ') + it.name;
            parent.appendChild(r);
            if (it.type === 'dir') {
                let isOpen = false, kids = null;
                r.onclick = async () => {
                    isOpen = !isOpen;
                    if (isOpen) {
                        r.textContent = '▾ ' + it.name;
                        kids = document.createElement('div');
                        kids.style.marginLeft = '12px';
                        parent.insertBefore(kids, r.nextSibling);
                        await renderDir(kids, it.path);
                    } else { r.textContent = '▸ ' + it.name; if (kids) kids.remove(); }
                };
            } else r.onclick = () => openFile(it.path);
        });
    }

    async function openFile(path) {
        const j = await (await fetch(BASE + '/api/stints/' + cur + '/file?path=' +
            encodeURIComponent(path))).json();
        if (j.error) { add('sys', '(' + path + ': ' + j.error + ')'); return; }
        const ex = tabs.find(t => t.path === path);
        const body = j.content + (j.truncated ? '\n\n… [truncated]' : '');
        if (ex) ex.body = body; else tabs.push({ path: path, body: body });
        active = path; renderTabs(); renderViewer();
    }

    function renderTabs() {
        const box = $('wb-tabs');
        box.innerHTML = '';
        tabs.forEach(t => {
            const d = document.createElement('span');
            d.className = 'tab' + (t.path === active ? ' active' : '');
            d.textContent = t.path.split('/').pop() + ' ✕';
            d.onclick = ev => {
                if (ev.offsetX > d.offsetWidth - 16) {
                    tabs = tabs.filter(x => x !== t);
                    if (active === t.path) active = tabs.length ? tabs[tabs.length - 1].path : null;
                } else active = t.path;
                renderTabs(); renderViewer();
            };
            box.appendChild(d);
        });
    }

    function renderViewer() {
        const t = tabs.find(t => t.path === active);
        $('wb-viewer').textContent = t ? t.body : '';
    }

    function dl() {
        if (!cur) return;
        const a = document.createElement('a');
        a.href = BASE + '/api/stints/' + cur + '/download?zip=1';
        a.download = cur + '.zip';
        document.body.appendChild(a); a.click(); a.remove();
    }
    window.wbDl = dl;

    /* ---------------- thought-bubble auto-scroll (workbench dock) ----------------
     * When buddy's listening/thinking text overflows the docked bubble, cycle it:
     * pause 1s at top → scroll down at a fixed rate → pause 1s at bottom → repeat.
     * Resets to the top whenever the text changes. */
    const TS_RATE = 22;           // px/sec
    const TS_PAUSE = 1000;        // ms hold at each end
    let tsPhase = 'top', tsT0 = 0, tsLast = '';
    function thoughtScroll(ts) {
        const box = document.getElementById('thought-text');
        if (box) {                    // same behavior on station + workbench
            const txt = box.textContent;
            if (txt !== tsLast) { tsLast = txt; tsPhase = 'top'; tsT0 = ts; box.scrollTop = 0; }
            const max = box.scrollHeight - box.clientHeight;
            if (max <= 2) {
                box.scrollTop = 0;
            } else if (tsPhase === 'top') {
                if (ts - tsT0 >= TS_PAUSE) { tsPhase = 'scroll'; tsT0 = ts; }
            } else if (tsPhase === 'scroll') {
                box.scrollTop = Math.min(max, (ts - tsT0) / 1000 * TS_RATE);
                if (box.scrollTop >= max) { tsPhase = 'bottom'; tsT0 = ts; }
            } else if (tsPhase === 'bottom') {
                if (ts - tsT0 >= TS_PAUSE) { tsPhase = 'top'; tsT0 = ts; box.scrollTop = 0; }
            }
        }
        requestAnimationFrame(thoughtScroll);
    }

    /* ---------------- boot ---------------- */

    document.addEventListener('DOMContentLoaded', () => {
        const inp = $('wb-inp');
        if (inp) inp.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
        });
        // Any interaction inside the workbench refreshes the stand-by hold.
        const panel = $('wb-panel');
        if (panel) {
            panel.addEventListener('click', standByTouch);
            panel.addEventListener('keydown', standByTouch);
        }
        // Pause the bench timers when the tab/window is hidden; resume on return.
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && wbOn()) standByTouch();
        });
        if (localStorage.getItem('eidosTab') === 'wb') wbTab(true);
        requestAnimationFrame(thoughtScroll);
    });
})();
