(() => {
    const button = document.getElementById('downloadBtn');
    const section = document.createElement('div');
    section.className = 'input-group';
    section.style.marginTop = '20px';
    section.innerHTML = `<label for="destinationFolder">Speicherordner auf der SSD</label>
<select id="destinationFolder" style="width:100%;padding:12px;background:#0f172a;color:#f8fafc;border:1px solid #334155;border-radius:8px" disabled><option value="">Ordner werden geladen …</option></select>
<div class="batch-row"><input type="text" id="newFolderName" placeholder="Neuer Unterordner im ausgewählten Ordner" style="flex:1;min-width:160px"><button type="button" class="btn-small" id="createFolderBtn">Ordner anlegen</button><button type="button" class="btn-small" id="refreshFoldersBtn">Aktualisieren</button></div>
<span id="folderStatus" role="status" style="font-size:.85rem"></span>`;
    button.before(section);
    const select = document.getElementById('destinationFolder');
    const status = document.getElementById('folderStatus');
    window.foldersReady = false;
    async function load(selected = select.value) {
        window.foldersReady = false;
        select.disabled = true;
        try {
            const response = await fetch('/folders');
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Ordner konnten nicht geladen werden');
            select.replaceChildren(new Option('Hauptordner (audiobooks)', ''));
            for (const path of data.folders) select.add(new Option(path, path));
            select.value = selected;
            if (select.selectedIndex < 0) select.value = '';
            select.disabled = false;
            window.foldersReady = true;
            status.textContent = 'Ziel: /mnt/ssd/audiobooks' + (select.value ? '/' + select.value : '');
        } catch (error) { status.textContent = error.message; }
    }
    select.addEventListener('change', () => {
        status.textContent = 'Ziel: /mnt/ssd/audiobooks' + (select.value ? '/' + select.value : '');
    });
    document.getElementById('refreshFoldersBtn').onclick = () => load();
    document.getElementById('createFolderBtn').onclick = async function () {
        if (!window.foldersReady) return;
        const input = document.getElementById('newFolderName');
        const name = input.value.trim();
        if (!name) { status.textContent = 'Bitte einen Ordnernamen eingeben.'; return; }
        this.disabled = true;
        try {
            const response = await fetch('/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({parent:select.value, name})});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Ordner konnte nicht angelegt werden');
            input.value = '';
            await load(data.folder);
        } catch (error) { status.textContent = error.message; }
        finally { this.disabled = false; }
    };
    load();
})();
