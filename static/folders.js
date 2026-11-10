(() => {
    const button = document.getElementById('downloadBtn');
    const section = document.createElement('div');
    section.className = 'input-group'; section.style.marginTop = '20px';
    section.innerHTML = `<label>Speicherordner auf der SSD</label><div id="folderTabs" class="folder-tabs" role="tablist" aria-label="Audiobook-Ordner"></div><select id="destinationFolder" aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)" disabled><option value="">Hauptordner (audiobooks)</option></select><div class="batch-row"><input type="text" id="newFolderName" placeholder="Neuer Ordner direkt im Hauptordner (oder Pfad Sub/Unterordner)" style="flex:1;min-width:160px"><button type="button" class="btn-small" id="createFolderBtn">+ Neuer Ordner</button><button type="button" class="btn-small" id="refreshFoldersBtn">Aktualisieren</button></div><span id="folderStatus" role="status" style="font-size:.85rem"></span>`;
    button.before(section);
    const select = document.getElementById('destinationFolder'), status = document.getElementById('folderStatus');
    const tabs = document.getElementById('folderTabs'); window.foldersReady = false;
    function renderTabs() {
        tabs.replaceChildren();
        for (const option of select.options) {
            const tab = document.createElement('button'); tab.type = 'button'; tab.className = 'folder-tab' + (option.value === select.value ? ' active' : ''); tab.setAttribute('role','tab'); tab.setAttribute('aria-selected', String(option.value === select.value));
            tab.textContent = option.value || 'Hauptordner (audiobooks)';
            tab.onclick = () => { select.value = option.value; renderTabs(); status.textContent = 'Ziel: /mnt/ssd/audiobooks' + (select.value ? '/' + select.value : ''); };
            tabs.appendChild(tab);
        }
    }
    async function load(selected = select.value) {
        window.foldersReady = false; select.disabled = true;
        try {
            const response = await fetch('/folders'); const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Ordner konnten nicht geladen werden');
            select.replaceChildren(new Option('Hauptordner (audiobooks)', ''));
            for (const path of data.folders) select.add(new Option(path, path));
            select.value = selected; if (select.selectedIndex < 0) select.value = '';
            renderTabs(); select.disabled = false; window.foldersReady = true;
            status.textContent = 'Ziel: /mnt/ssd/audiobooks' + (select.value ? '/' + select.value : '');
        } catch (error) { status.textContent = error.message; }
    }
    document.getElementById('refreshFoldersBtn').onclick = () => load();
    document.getElementById('createFolderBtn').onclick = async function () {
        if (!window.foldersReady) return;
        const input = document.getElementById('newFolderName'), name = input.value.trim();
        if (!name) { status.textContent = 'Bitte einen Ordnernamen oder einen relativen Pfad eingeben.'; return; }
        this.disabled = true;
        try {
            const response = await fetch('/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({parent:'', name})});
            const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Ordner konnte nicht angelegt werden');
            input.value = ''; await load(data.folder);
        } catch (error) { status.textContent = error.message; } finally { this.disabled = false; }
    };
    load();
})();
