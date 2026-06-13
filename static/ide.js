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
    let petHome = null, crewHome = null;   // where creature-box / crew live on the station tab

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
        const box = $('creature-box'), crew = $('wb-crew');
        if (box) {
            if (on) {
                petHome = petHome || box.parentElement;
                crewHome = crewHome || (crew && crew.parentElement);
                const dock = $('wb-pet');
                if (dock) dock.appendChild(box);
                // crew sits directly under the ASCII buddy (not below the stats/titles)
                const art = document.getElementById('creature-art');
                if (art && crew) art.insertAdjacentElement('afterend', crew);
            } else {
                // pull the crew out of creature-box BEFORE it returns to the station,
                // else it would ride along and show on the station tab.
                if (crew && crewHome) crewHome.insertBefore(crew, $('wb-stints'));
                if (petHome) petHome.appendChild(box);
            }
        }
        if (on) {
            listStints();
            if (!railTimer) railTimer = setInterval(listStints, 2500);
            if (!crewTimer) crewTimer = setInterval(crewRender, 250);
            standByTouch();                       // arriving = present
        } else {
            if (railTimer) { clearInterval(railTimer); railTimer = null; }
            if (crewTimer) { clearInterval(crewTimer); crewTimer = null; }
            standByRelease();                     // leaving the bench frees the buddy
        }
        if (window.refreshThought) window.refreshThought();   // standby line flips in at once
    }
    window.wbTab = wbTab;

    /* ---------------- crew bench (one mini per stint) ---------------- */

    // The crew = the subagents the CURRENT stint's main agent spawned (its mini-buddies).
    // They're assigned to that task and stay with it; viewing a different stint shows that
    // stint's crew. Glyph reflects each subagent's real status. Spinner animates here (250ms);
    // membership refreshes from /api/stints (listStints) on subagent events.
    const SPIN = ['|', '/', '-', '\\'];
    function crewRender() {
        const pre = $('wb-crew');
        if (!pre) return;
        const sp = (window.Creature && Creature.miniSprite) ? Creature.miniSprite() : '(o)';
        const t = Date.now();
        const s = stints.find(x => x.id === cur);
        const subs = (s && s.subagents) || [];
        if (subs.length) {
            let line = '', tip = [];
            for (const a of subs) {
                const g = a.status === 'done' ? '✓' : a.status === 'error' ? '✗'
                    : SPIN[Math.floor(t / 200) % 4];
                line += '  ' + sp + g;
                tip.push(a.desc + ' — ' + a.status);
            }
            pre.textContent = line;
            pre.title = tip.join(' · ');
        } else if (s && s.turn_active) {
            pre.textContent = '  ' + sp + SPIN[Math.floor(t / 200) % 4] + ' solo';
            pre.title = s.title + ' — main agent working (no subagents spawned)';
        } else {
            pre.textContent = '  no crew yet';
            pre.title = s ? s.title + ' — idle' : 'pick a stint';
        }
        // tab badge reflects ANY active agent across stints (visible from the station tab)
        const busy = stints.some(x => x.turn_active ||
            (x.subagents || []).some(a => a.status !== 'done'));
        const wb = $('tab-wb');
        if (wb) wb.textContent = busy ? 'workbench ⚒' : 'workbench';
        if (busy) standByTouch();   // work in flight → buddy stays present even if you're idle
    }

    // Big buddy "looks at whatever you're looking at": the focus line for the thought bubble.
    window.wbFocus = function () {
        if (!wbOn()) return null;
        const s = stints.find(x => x.id === cur);
        if (!s) return null;
        const running = (s.subagents || []).filter(a => a.status !== 'done').length;
        if (running > 0) return 'watching “' + s.title + '” — ' + running +
            ' subagent' + (running > 1 ? 's' : '') + ' on it';
        if (s.turn_active) return 'watching “' + s.title + '” — the agent is working';
        return null;   // idle task → fall back to the persona standby line
    };

    /* ---------------- stints rail ---------------- */

    // True when the log is scrolled to (near) the bottom. If you've scrolled up to read,
    // new/streaming content must NOT yank you down — manual scroll wins over the stream.
    function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 48; }

    function add(cls, txt) {
        const log = $('wb-log');
        const stick = nearBottom(log);
        const d = document.createElement('div');
        d.className = cls; d.textContent = txt;
        log.appendChild(d);
        if (stick) log.scrollTop = log.scrollHeight;
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
            const label = document.createElement('span');
            label.className = 'stint-label';
            label.textContent = (s.status === 'running' ? '● ' : '○ ') + s.title;
            label.onclick = () => open(s.id, s.title, s.status);
            const del = document.createElement('span');
            del.className = 'stint-del'; del.textContent = '✕'; del.title = 'Delete this stint';
            del.onclick = (e) => { e.stopPropagation(); delStint(s.id, s.title); };
            d.appendChild(label); d.appendChild(del);
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

    async function delStint(id, title) {
        if (!confirm('Delete stint “' + (title || id) + '”?\nThis permanently removes its chat, files, and session.')) return;
        try { await fetch(BASE + '/api/stints/' + id + '/delete', { method: 'POST' }); }
        catch (e) { add('sys', 'delete failed: ' + e); return; }
        if (cur === id) {                       // was viewing it → clear the panes
            cur = null; if (es) { es.close(); es = null; }
            $('wb-log').innerHTML = '<div class="sys">stint deleted.</div>';
            tabs = []; active = null; renderTabs(); $('wb-viewer').textContent = '';
            $('wb-curname').textContent = ''; $('wb-send').disabled = true; $('wb-dl').disabled = true;
        }
        listStints();
    }
    window.delStint = delStint;

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
        if (curView() === 'preview') loadPreview();
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
                const log = $('wb-log');
                const stick = nearBottom(log);
                if (!curText) curText = add('msg-pi', sprite() + ' ▸ ');
                curText.textContent += a.delta || '';
                if (stick) log.scrollTop = log.scrollHeight;
            }
        } else if (t === 'tool_execution_start') {
            const ar = ev.args || {};
            const d = ar.path || ar.command || ar.pattern || '';
            add('tool', '⚙ ' + ev.toolName + (d ? ' ' + String(d).slice(0, 80) : ''));
            curText = null;
            if ((ev.toolName === 'write' || ev.toolName === 'edit') && ar.path) pendWrite = ar.path;
        } else if (t === 'tool_execution_end') {
            if (ev.isError) add('tool', '  ✗ error');
            else if (pendWrite) {
                loadTree(); openFile(pendWrite);
                if (curView() === 'preview') loadPreview();   // live-refresh the preview as files land
                pendWrite = null;
            }
            listStints();                          // subagent spawn/done may have changed the crew
        } else if (t === 'extension_ui_request') {
            listStints();                          // running-agent count changed
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

    /* ---------------- code-pane toggle (responsive) ----------------
     * Three columns don't fit a half-screen window. The code pane collapses via the
     * wb-nocode body class; on narrow widths it defaults off (chat gets the floor) and
     * the header button swaps it in. Once you click, your choice sticks. */
    function curView() {
        if (document.body.classList.contains('wb-nocode')) return 'chat';
        return document.body.classList.contains('wb-pv') ? 'preview' : 'code';
    }
    function syncViewBtns() {
        const v = curView();
        [['sw-chat', 'chat'], ['sw-code', 'code'], ['sw-pv', 'preview']].forEach(p => {
            const b = $(p[0]); if (b) b.classList.toggle('on', v === p[1]);
        });
    }
    function setView(view, persist) {
        document.body.classList.toggle('wb-nocode', view === 'chat');     // chat-only
        document.body.classList.toggle('wb-pv', view === 'preview');      // right pane = preview
        if (persist) localStorage.setItem('wbView', view);
        if (view === 'preview') loadPreview();
        syncViewBtns();
    }
    function wbShow(view) { setView(view, true); }
    window.wbShow = wbShow;
    function applyViewDefault() {
        let v = localStorage.getItem('wbView');
        if (!v) v = (window.innerWidth < 1080) ? 'chat' : 'code';   // auto until you choose
        setView(v, false);
    }

    /* ---------------- preview: html page / image / 3D model ---------------- */
    let _pvUrl = '', _mvLoaded = false;
    function previewKind(p) {
        const e = (p.split('.').pop() || '').toLowerCase();
        if (['html', 'htm'].indexOf(e) >= 0) return 'html';
        if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'].indexOf(e) >= 0) return 'image';
        if (['glb', 'gltf'].indexOf(e) >= 0) return 'model';
        if (e === 'pdf') return 'pdf';
        return null;
    }
    async function listAllFiles(path, depth) {
        depth = depth || 0; if (depth > 4 || !cur) return [];
        let out = [];
        try {
            const j = await (await fetch(BASE + '/api/stints/' + cur + '/tree?path=' +
                encodeURIComponent(path))).json();
            for (const it of (j.items || [])) {
                if (it.type === 'dir') out = out.concat(await listAllFiles(it.path, depth + 1));
                else out.push(it.path);
            }
        } catch (e) { /* unreachable IDE service → empty */ }
        return out;
    }
    async function pickPreviewTarget() {
        if (active && previewKind(active)) return { path: active, kind: previewKind(active) };
        const files = await listAllFiles('');
        const html = files.find(f => /(^|\/)index\.html$/i.test(f)) || files.find(f => /\.html?$/i.test(f));
        if (html) return { path: html, kind: 'html' };
        const img = files.find(f => previewKind(f) === 'image'); if (img) return { path: img, kind: 'image' };
        const mdl = files.find(f => previewKind(f) === 'model'); if (mdl) return { path: mdl, kind: 'model' };
        return null;
    }
    function pvMsg(t) { $('wb-pvbody').innerHTML = '<div class="sys" style="padding:16px">' + t + '</div>'; }
    async function loadPreview() {
        const what = $('wb-pvwhat');
        if (!cur) { pvMsg('pick a stint to preview its output.'); if (what) what.textContent = ''; return; }
        const t = await pickPreviewTarget();
        if (!t) {
            pvMsg('nothing to preview yet — when the crew writes an HTML page, image, or 3D model it shows here.');
            if (what) what.textContent = ''; return;
        }
        const url = BASE + '/api/stints/' + cur + '/raw/' + t.path.split('/').map(encodeURIComponent).join('/');
        _pvUrl = url;
        if (what) what.textContent = t.path + ' · ' + t.kind;
        const body = $('wb-pvbody');
        if (t.kind === 'html' || t.kind === 'pdf') {
            body.innerHTML = '';
            const f = document.createElement('iframe');
            f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-modals allow-popups');
            f.src = url; body.appendChild(f);
        } else if (t.kind === 'image') {
            body.innerHTML = ''; const im = document.createElement('img'); im.src = url; body.appendChild(im);
        } else if (t.kind === 'model') {
            body.innerHTML = '<model-viewer src="' + url + '" camera-controls auto-rotate></model-viewer>';
            ensureModelViewer();
        }
    }
    function ensureModelViewer() {
        if (_mvLoaded || (window.customElements && customElements.get('model-viewer'))) { _mvLoaded = true; return; }
        _mvLoaded = true;
        const s = document.createElement('script'); s.type = 'module';
        s.src = 'https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js';
        s.onerror = () => pvMsg('3D viewer needs the model-viewer CDN, which is unreachable here. The .glb is still downloadable.');
        document.head.appendChild(s);
    }
    function wbPvRefresh() { loadPreview(); }
    function wbPvOpen() { if (_pvUrl) window.open(_pvUrl, '_blank'); }
    window.wbPvRefresh = wbPvRefresh;
    window.wbPvOpen = wbPvOpen;

    /* ---------------- boot ---------------- */

    document.addEventListener('DOMContentLoaded', () => {
        const inp = $('wb-inp');
        if (inp) inp.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
        });
        const nt = $('wb-newtitle');
        if (nt) nt.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); newStint(); }
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
        applyViewDefault();
        // Re-evaluate on resize only while you're in auto mode (haven't picked a view).
        window.addEventListener('resize', () => {
            if (localStorage.getItem('wbView') !== null) return;
            setView(window.innerWidth < 1080 ? 'chat' : 'code', false);
        });
        if (localStorage.getItem('eidosTab') === 'wb') wbTab(true);
        requestAnimationFrame(thoughtScroll);
    });
})();
