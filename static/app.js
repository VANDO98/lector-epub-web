let state = {
    currentView: 'library',
    books: [],
    driveFiles: [],
    localDriveIds: new Set(),
    activeBookId: null,
    capitulos: [],
    allFrags: [],
    capIdx: 0,
    fragIdx: 0,
    voice: "es-MX-DaliaNeural",
    audioObj: null,
    audioNext: null,
    isPlaying: false
};

// --- ROUTING ---
function navTo(viewId) {
    document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
    document.getElementById(`view-${viewId}`).classList.add('active');
    
    // Control mini-player visibility
    const mini = document.getElementById('mini-player');
    if (viewId !== 'reader' && state.audioObj && !state.audioObj.paused) {
        mini.classList.add('active');
        updateMiniPlayer();
    } else {
        mini.classList.remove('active');
    }
    state.currentView = viewId;
    if (viewId === 'library') loadLibrary();
}

// --- UTILS ---
function showLoad(msg="Cargando...") { 
    document.getElementById('loading-msg').textContent = msg;
    document.getElementById('loading').classList.add('show'); 
}
function hideLoad() { document.getElementById('loading').classList.remove('show'); }
function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg; t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 3000);
}
function escapeHtml(s) {
    return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// --- INIT ---
document.addEventListener('DOMContentLoaded', () => {
    loadLibrary();
    loadVoices();
    checkDriveStatus();
    
    // Play button
    document.getElementById('play-btn').onclick = () => {
        if (state.isPlaying) stopAudio();
        else playFragment(state.capIdx, state.fragIdx, true);
    };
});

// --- LIBRARY ---
async function loadLibrary() {
    showLoad();
    try {
        const r = await fetch('/api/library');
        state.books = await r.json();
        renderLibrary(state.books);
    } catch(e) { toast("Error cargando biblioteca"); }
    hideLoad();
}

function renderLibrary(books) {
    const grid = document.getElementById('library-grid');
    grid.innerHTML = '';
    books.forEach(b => {
        const pct = b.cap_idx > 0 ? 30 : 0; // Simple fallback calculation, would need total chapters for real pct
        const card = document.createElement('div');
        card.className = 'b-card';
        card.onclick = () => openDetails(b);
        card.innerHTML = `
            <img class="b-cover" src="/api/library/${b.id}/cover" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\'><rect width=\\'100\\' height=\\'140\\' fill=\\'%23334155\\'/></svg>'">
            <div class="b-info">
                <div>
                    <div class="b-title">${escapeHtml(b.title)}</div>
                    <div class="b-author">${escapeHtml(b.author)}</div>
                    <div class="b-meta">17 cap. <div class="b-meta-dot"></div> EPUB <div class="b-meta-dot"></div> 29%</div>
                </div>
                <div class="b-action">
                    Seguir escuchando •
                    <span>Capítulo ${b.cap_idx + 1}</span>
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

function filterLibrary() {
    const q = document.getElementById('lib-search').value.toLowerCase();
    renderLibrary(state.books.filter(b => b.title.toLowerCase().includes(q) || b.author.toLowerCase().includes(q)));
}

async function uploadLocal(input) {
    if(!input.files[0]) return;
    showLoad("Subiendo...");
    const fd = new FormData();
    fd.append("file", input.files[0]);
    try {
        await fetch('/api/library/add', {method:'POST', body:fd});
        toast("Libro añadido");
        loadLibrary();
    } catch(e) { toast("Error"); }
    hideLoad();
    input.value = '';
}

// --- DRIVE ---
let isDriveConnected = false;

async function checkDriveStatus() {
    try {
        const r = await fetch('/auth/status');
        const res = await r.json();
        isDriveConnected = res.connected;
    } catch(e) {}
}
function loginDrive() { window.location.href = '/auth/login'; }
async function syncDrive() {
    showLoad("Sincronizando...");
    try {
        const r = await fetch('/api/drive/sync', {method:'POST'});
        if(!r.ok) throw new Error();
        toast("Sincronización completada");
    } catch(e) { toast("Error sincronizando"); }
    hideLoad();
}

async function showDriveModal() {
    showLoad();
    document.getElementById('drive-search-input').value = '';
    
    // Check if connected first
    await checkDriveStatus();
    
    if (!isDriveConnected) {
        document.getElementById('drive-files-list').innerHTML = '<p style="text-align:center; margin-top: 1rem; color:var(--text-muted);">No estás conectado a Google Drive.</p>';
        document.getElementById('drive-modal').classList.add('show');
        hideLoad();
        return;
    }
    
    try {
        const rDrive = await fetch('/api/drive/files');
        if (!rDrive.ok) {
            if (rDrive.status === 401) {
                isDriveConnected = false;
                document.getElementById('drive-files-list').innerHTML = '<p style="text-align:center; margin-top: 1rem; color:var(--text-muted);">La sesión expiró. Reconecta tu cuenta.</p>';
                document.getElementById('drive-modal').classList.add('show');
                hideLoad();
                return;
            }
            throw new Error();
        }
        
        const rLocal = await fetch('/api/library');
        state.driveFiles = await rDrive.json();
        const libBooks = await rLocal.json();
        state.localDriveIds = new Set(libBooks.filter(b => b.source === 'drive' && b.drive_id).map(b => b.drive_id));
        renderDriveFiles(state.driveFiles);
        document.getElementById('drive-modal').classList.add('show');
    } catch(e) { toast("Error cargando archivos de Drive."); }
    hideLoad();
}
function filterDriveFiles() {
    const q = document.getElementById('drive-search-input').value.toLowerCase();
    renderDriveFiles(state.driveFiles.filter(f => f.name.toLowerCase().includes(q)));
}
function renderDriveFiles(files) {
    const list = document.getElementById('drive-files-list');
    list.innerHTML = '';
    files.forEach(f => {
        const isImported = state.localDriveIds.has(f.id);
        const div = document.createElement('div');
        div.className = 'drive-item';
        div.innerHTML = `
            <div class="drive-item-name" style="opacity: ${isImported ? '0.5' : '1'}">${escapeHtml(f.name)}</div>
            ${isImported 
                ? '<span style="color: var(--accent); font-size:0.8rem; font-weight:600;">✓</span>'
                : `<button class="btn btn-primary btn-small" onclick="importDriveFile('${f.id}')">Importar</button>`
            }
        `;
        list.appendChild(div);
    });
}
async function importDriveFile(id) {
    document.getElementById('drive-modal').classList.remove('show');
    showLoad("Importando...");
    try {
        await fetch(`/api/drive/import/${id}`, {method:'POST'});
        toast("Importado");
        loadLibrary();
    } catch(e) { toast("Error importando"); }
    hideLoad();
}


// --- DETAILS ---
async function openDetails(book) {
    state.activeBookId = book.id;
    showLoad();
    try {
        const r = await fetch(`/api/library/${book.id}/open`);
        const data = await r.json();
        state.capitulos = data.capitulos;
        state.capIdx = data.cap_idx;
        state.fragIdx = data.frag_idx;
        
        // Populate UI
        document.getElementById('detail-title').textContent = data.title;
        document.getElementById('detail-author').textContent = book.author || 'Desconocido';
        const coverUrl = `/api/library/${book.id}/cover`;
        document.getElementById('detail-cover').src = coverUrl;
        document.getElementById('reader-bg').style.backgroundImage = `url(${coverUrl})`;
        
        const tot = state.capitulos.length;
        document.getElementById('detail-chap-count').textContent = tot;
        document.getElementById('detail-total-count').textContent = tot;
        
        // Simular tiempo y lectura
        const readCount = state.capIdx;
        document.getElementById('detail-read-count').textContent = readCount;
        
        const pct = tot > 0 ? Math.round((readCount / tot) * 100) : 0;
        document.getElementById('detail-progress-pct').textContent = `${pct}%`;
        document.getElementById('detail-ring').style.strokeDasharray = `${pct}, 100`;
        document.getElementById('detail-prog-bar').style.width = `${pct}%`;
        
        const currentCap = state.capitulos[state.capIdx];
        document.getElementById('detail-current-chapter').textContent = `Capítulo ${state.capIdx+1}: ${currentCap ? currentCap.titulo : ''}`;
        
        // Render chapters list
        const list = document.getElementById('detail-chapters');
        list.innerHTML = '';
        state.capitulos.forEach((c, i) => {
            const isRead = i < state.capIdx;
            const timeEst = Math.max(1, Math.round(c.num_fragmentos * 120 / 150)); // 120 words per frag / 150 wpm
            const div = document.createElement('div');
            div.className = `chap-item ${isRead ? 'read' : ''}`;
            div.onclick = () => { state.capIdx = i; state.fragIdx = 0; navToReader(); };
            div.innerHTML = `
                <div class="chap-left">
                    <div class="chap-num ${isRead ? 'read' : ''}">
                        ${isRead ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px;"><polyline points="20 6 9 17 4 12"/></svg>' : i+1}
                    </div>
                    <div class="chap-title">${escapeHtml(c.titulo)}</div>
                </div>
                <div class="chap-right">
                    <div class="chap-time">${timeEst}m</div>
                    <svg class="check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
            `;
            list.appendChild(div);
        });
        
        let totalMins = state.capitulos.reduce((acc, c) => acc + Math.max(1, Math.round(c.num_fragmentos * 120 / 150)), 0);
        let h = Math.floor(totalMins/60);
        let m = totalMins % 60;
        document.getElementById('detail-time').textContent = `${h}h ${m}m`;

        // Lógica de Favoritos
        let favs = JSON.parse(localStorage.getItem('favBooks') || '[]');
        const isFav = favs.includes(book.id);
        const favBtn = document.getElementById('btn-add-fav');
        if(favBtn) {
            favBtn.style.color = isFav ? 'var(--accent)' : 'var(--text-main)';
            favBtn.onclick = () => {
                let currentFavs = JSON.parse(localStorage.getItem('favBooks') || '[]');
                if (currentFavs.includes(book.id)) {
                    currentFavs = currentFavs.filter(id => id !== book.id);
                    favBtn.style.color = 'var(--text-main)';
                    toast("Eliminado de Favoritos");
                } else {
                    currentFavs.push(book.id);
                    favBtn.style.color = 'var(--accent)';
                    toast("Añadido a Favoritos");
                }
                localStorage.setItem('favBooks', JSON.stringify(currentFavs));
            };
        }

        navTo('details');
    } catch(e) { toast("Error abriendo libro"); }
    hideLoad();
}


// --- READER ---
async function navToReader() {
    navTo('reader');
    await loadCapData(state.capIdx, state.fragIdx, true);
}

async function loadCapData(cIdx, fIdx, autoPlay=false) {
    stopAudio();
    showLoad();
    state.capIdx = cIdx; state.fragIdx = fIdx;
    
    document.getElementById('reader-book-title').textContent = document.getElementById('detail-title').textContent;
    document.getElementById('reader-cap-title').textContent = state.capitulos[cIdx].titulo;
    document.getElementById('text-area').innerHTML = ''; 
    
    try {
        const numF = state.capitulos[cIdx].num_fragmentos;
        const frags = [];
        for(let i=0; i<numF; i++){
            const r = await fetch(`/api/texto/${state.activeBookId}/${cIdx}/${i}`);
            const d = await r.json();
            frags.push(d.texto);
        }
        state.allFrags = frags;
        
        renderAllText();
        
        saveProgress();
        await playFragment(cIdx, fIdx, autoPlay);
    } catch(e) { toast("Error cargando capítulo"); }
    hideLoad();
}

function renderAllText() {
    const area = document.getElementById('text-area');
    area.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'page-container-scroll';
    
    let html = '';
    for (let i = 0; i < state.allFrags.length; i++) {
        html += `<span class="frag" id="frag-${i}" onclick="playFragment(${state.capIdx}, ${i}, true)">${escapeHtml(state.allFrags[i])}</span>`;
    }
    div.innerHTML = html;
    area.appendChild(div);
}

function saveProgress() {
    const fd = new FormData();
    fd.append('cap_idx', state.capIdx);
    fd.append('frag_idx', state.fragIdx);
    fetch(`/api/library/${state.activeBookId}/progress`, {method:'POST', body: fd});
}

function getAudioUrl(c, f) { return `/api/audio/${state.activeBookId}/${c}/${f}?voz=${encodeURIComponent(state.voice)}`; }

async function playFragment(cIdx, fIdx, auto = false) {
    state.capIdx = cIdx; state.fragIdx = fIdx;
    
    // Highlight and scroll
    document.querySelectorAll('.frag.playing').forEach(el => el.classList.remove('playing'));
    const activeFrag = document.getElementById(`frag-${fIdx}`);
    if (activeFrag) {
        activeFrag.classList.add('playing');
        activeFrag.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    
    const tot = state.allFrags.length;
    document.getElementById('player-progress-fill').style.width = `${((fIdx)/Math.max(1, tot-1))*100}%`;
    document.getElementById('player-progress-thumb').style.left = `${((fIdx)/Math.max(1, tot-1))*100}%`;
    document.getElementById('player-time-curr').textContent = fIdx;
    document.getElementById('player-time-total').textContent = tot;
    
    const cur = state.audioObj || new Audio();
    state.audioObj = cur;
    cur.src = getAudioUrl(cIdx, fIdx);
    
    cur.onplay = () => {
        state.isPlaying = true;
        document.getElementById('play-btn').innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`;
    };
    cur.onpause = () => {
        state.isPlaying = false;
        document.getElementById('play-btn').innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" class="icon-play"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
    };
    
    cur.onended = () => {
        saveProgress();
        if (state.audioNext) {
            state.audioObj = state.audioNext;
            state.audioNext = null;
            playFragment(state.capIdx, state.fragIdx + 1, true);
        } else {
            nextFragment();
        }
    };
    
    if (auto) {
        try { await cur.play(); } 
        catch(e) { console.error(e); state.isPlaying=false; cur.onpause(); }
    }
    
    // Preload next
    if (fIdx + 1 < state.allFrags.length) {
        state.audioNext = new Audio(getAudioUrl(cIdx, fIdx + 1));
        state.audioNext.preload = "auto";
    }
}

function togglePlayPause() {
    if(!state.audioObj) return;
    if(state.audioObj.paused) state.audioObj.play();
    else state.audioObj.pause();
    updateMiniPlayer();
}

function updateMiniPlayer() {
    if (!state.activeBookId) return;
    const book = state.books.find(b => b.id === state.activeBookId);
    if (book) {
        document.getElementById('mini-title').textContent = book.title;
        document.getElementById('mini-cover-img').src = `/api/library/${book.id}/cover`;
    }
    const cap = state.capitulos[state.capIdx];
    if (cap) {
        document.getElementById('mini-cap').textContent = `Capítulo ${state.capIdx+1}`;
    }
    
    const playIcon = document.getElementById('mini-play-icon');
    const pauseIcon = document.getElementById('mini-pause-icon');
    if (state.audioObj && !state.audioObj.paused) {
        playIcon.style.display = 'none';
        pauseIcon.style.display = 'block';
    } else {
        playIcon.style.display = 'block';
        pauseIcon.style.display = 'none';
    }
}

function stopAudio() {
    if (state.audioObj) {
        state.audioObj.pause();
        state.isPlaying = false;
    }
}

function prevFragment() {
    if (state.fragIdx > 0) playFragment(state.capIdx, state.fragIdx - 1, true);
    else if (state.capIdx > 0) loadCapData(state.capIdx - 1, 0, true); // Should go to last frag ideally
}
function nextFragment() {
    if (state.fragIdx < state.allFrags.length - 1) playFragment(state.capIdx, state.fragIdx + 1, true);
    else if (state.capIdx < state.capitulos.length - 1) loadCapData(state.capIdx + 1, 0, true);
}

// --- VOICES ---
async function loadVoices() {
    try {
        const r = await fetch('/api/voces');
        const v = await r.json();
        const s = document.getElementById('voice-select');
        for (let [name, id] of Object.entries(v)) {
            let opt = document.createElement('option');
            opt.value = id; opt.textContent = name;
            if (id === state.voice) opt.selected = true;
            s.appendChild(opt);
        }
        s.onchange = (e) => { state.voice = e.target.value; };
    } catch(e) {}
}
