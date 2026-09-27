(() => {
    'use strict';
    // Draft workspace: live page previews, per-page edits and multi-round AI edits.
    // Published versions are never modified; the first change opens a draft.
    const names = {modify:'修改', delete:'删除', add:'新增'};

    function open(initial, {packages=[], onChange=()=>{}} = {}) {
        const ui = window.PackageUI;
        const {node, request, families, api} = ui;
        let pkg = initial;
        let selected = pkg.manifest.components[0]?.id;
        let busy = false;
        let controller = null;
        const undo = [];
        const rounds = [];
        const live = new Map(); // component id -> streamed html not yet saved

        const modal = ui.dialog('');
        modal.classList.add('package-workspace');
        const head = modal.querySelector('header');
        const title = node('div', undefined, 'pw-title');
        const nameEl = node('h2');
        const meta = node('span', '', 'pw-meta');
        const renameBtn = ui.button('重命名', rename);
        renameBtn.classList.add('pw-quiet');
        title.append(nameEl, meta, renameBtn);
        head.replaceChild(title, head.querySelector('h2'));

        const banner = node('div', '', 'pw-banner');
        const pagesHead = node('div', undefined, 'pw-rail-head');
        const pageCount = node('span');
        const addToggle = ui.button('+ 新增页面', () => { addForm.hidden = !addForm.hidden; if (!addForm.hidden) addText.focus(); });
        pagesHead.append(pageCount, addToggle);
        const addForm = node('form', undefined, 'pw-add');
        addForm.hidden = true;
        const addRef = node('select'); addRef.setAttribute('aria-label', '参考版式');
        const addText = node('textarea'); addText.rows = 3; addText.maxLength = 4000;
        addText.placeholder = '描述新页面，例如：左侧大图、右侧 3 个要点的图文页';
        addText.setAttribute('aria-label', '新页面要求');
        const addSubmit = node('button', 'AI 生成页面', 'primary'); addSubmit.type = 'submit';
        const addRefLabel = node('label', '参考版式'); addRefLabel.append(addRef);
        addForm.append(addRefLabel, addText, addSubmit);
        const list = node('ol', undefined, 'pw-pages');
        const rail = node('aside', undefined, 'pw-rail'); rail.setAttribute('aria-label', '页面列表');
        rail.append(pagesHead, addForm, list);

        const stageFrame = node('iframe'); stageFrame.title = '页面预览'; stageFrame.setAttribute('sandbox', '');
        const stagePreview = ui.fitPreview(stageFrame); stagePreview.classList.add('pw-stage-preview');
        const pageLabel = node('div', '', 'pw-page-label');
        const pageTools = node('div', undefined, 'pw-page-tools');
        const upBtn = ui.button('上移', () => move(-1));
        const downBtn = ui.button('下移', () => move(1));
        const dupBtn = ui.button('复制', duplicate);
        const delBtn = ui.button('删除', removePage); delBtn.classList.add('danger');
        pageTools.append(upBtn, downBtn, dupBtn, delBtn);
        const pageBar = node('div', undefined, 'pw-page-bar'); pageBar.append(pageLabel, pageTools);
        const pageForm = node('form', undefined, 'pw-page-ai');
        const pageInput = node('input'); pageInput.maxLength = 4000;
        pageInput.placeholder = '修改此页，例如：标题放大，改为左右分栏';
        pageInput.setAttribute('aria-label', '此页修改要求');
        const pageSubmit = node('button', 'AI 修改此页', 'primary'); pageSubmit.type = 'submit';
        pageForm.append(pageInput, pageSubmit);
        const stage = node('section', undefined, 'pw-stage');
        stage.append(stagePreview, pageBar, pageForm);

        const chat = node('aside', undefined, 'pw-chat'); chat.setAttribute('aria-label', 'AI 多轮编辑');
        const chatHead = node('div', undefined, 'pw-chat-head');
        const undoBtn = ui.button('撤销', undoLast); undoBtn.classList.add('pw-quiet');
        chatHead.append(node('h3', 'AI 多轮编辑'), undoBtn);
        const log = node('ol', undefined, 'pw-log');
        const chatForm = node('form', undefined, 'pw-chat-form');
        const chatInput = node('textarea'); chatInput.rows = 4; chatInput.maxLength = 5000;
        chatInput.placeholder = '修改整套模板，例如：整体改成深蓝配色；删除图文页；新增一个时间线页面';
        chatInput.setAttribute('aria-label', '整套模板修改要求');
        const chatSubmit = node('button', '发送', 'primary'); chatSubmit.type = 'submit';
        const chatFoot = node('div', undefined, 'pw-chat-foot');
        chatFoot.append(node('small', 'Ctrl + Enter 发送 · 每轮会在上一轮结果上继续修改'), chatSubmit);
        chatForm.append(chatInput, chatFoot);
        chat.append(chatHead, log, chatForm);

        const body = node('div', undefined, 'pw-body'); body.append(rail, stage, chat);
        const progress = node('progress', undefined, 'package-progress'); progress.hidden = true;
        const status = node('div', '', 'package-status pw-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        const exportBtn = ui.button('导出模板包', () => ui.download(pkg));
        const publishBtn = ui.button('校验并发布', publish, true);
        const foot = node('footer', undefined, 'pw-foot');
        const footInfo = node('div', undefined, 'pw-foot-info'); footInfo.append(status, progress);
        const footActions = node('div', undefined, 'package-tools'); footActions.append(exportBtn, publishBtn);
        foot.append(footInfo, footActions);
        modal.append(banner, body, foot);

        modal.addEventListener('close', () => { controller?.abort(); onChange(); }, {once: true});
        chatInput.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); chatForm.requestSubmit(); } });
        chatForm.addEventListener('submit', e => { e.preventDefault(); aiRound(); });
        pageForm.addEventListener('submit', e => { e.preventDefault(); aiPage(); });
        addForm.addEventListener('submit', e => { e.preventDefault(); aiAdd(); });

        function say(text, error = false) { status.textContent = text; status.classList.toggle('error', error); }
        function previewUrl(id) { return `${api}/${pkg.id}/preview/${encodeURIComponent(id)}?h=${(pkg.content_hash || '').slice(0, 12)}`; }
        function component(id) { return pkg.manifest.components.find(c => c.id === id); }
        function label(c) { return `${families[c.family] || c.family} · ${c.description}`; }
        function setFrame(frame, id) {
            if (live.has(id)) { frame.removeAttribute('src'); frame.srcdoc = live.get(id); }
            else { frame.removeAttribute('srcdoc'); frame.src = previewUrl(id); }
        }
        function setBusy(value) {
            busy = value;
            for (const el of modal.querySelectorAll('.pw-body button:not(.pw-page-pick), .pw-body input, .pw-body textarea, .pw-body select, .pw-foot button, .pw-banner button')) el.disabled = value;
            renameBtn.disabled = value;
            modal.classList.toggle('is-busy', value);
        }
        function renderHeader() {
            nameEl.textContent = pkg.template_name;
            meta.textContent = `v${pkg.version} · ${pkg.editable ? '草稿' : ({published:'已发布', retired:'已停用', validated:'已校验'}[pkg.status] || pkg.status)}`;
            renameBtn.hidden = !pkg.user_id;
            publishBtn.hidden = !pkg.editable;
            undoBtn.disabled = busy || !undo.length;
            const newer = packages.find(p => p.user_id && p.template_id === pkg.template_id && p.editable && p.version > pkg.version);
            banner.replaceChildren();
            if (pkg.editable) { banner.hidden = true; return; }
            banner.hidden = false;
            if (newer) {
                banner.append(node('span', `已有草稿 v${newer.version}。`), ui.button(`继续编辑草稿 v${newer.version}`, () => { pkg = newer; live.clear(); refresh(); say(''); }));
            } else {
                banner.append(node('span', pkg.user_id
                    ? `正在查看已发布的 v${pkg.version}。首次修改会自动创建新草稿，使用此版本的项目不受影响。`
                    : '这是系统模板包。首次修改会另存为你的模板包草稿。'));
            }
        }
        function renderReferences() {
            addRef.replaceChildren();
            for (const c of pkg.manifest.components) { const o = node('option', label(c)); o.value = c.id; addRef.append(o); }
            addRef.value = selected || addRef.value;
        }
        function renderList() {
            const comps = pkg.manifest.components;
            pageCount.textContent = `页面 ${comps.length}`;
            list.querySelectorAll('.package-preview-holder').forEach(el=>el.disposePreview?.());
            list.replaceChildren();
            comps.forEach((c, i) => {
                const item = node('li', undefined, 'pw-page');
                item.dataset.id = c.id;
                const pick = node('button', undefined, 'pw-page-pick'); pick.type = 'button';
                pick.setAttribute('aria-current', c.id === selected ? 'true' : 'false');
                const thumb = node('iframe'); thumb.loading = 'lazy'; thumb.tabIndex = -1; thumb.setAttribute('sandbox', ''); thumb.title = label(c);
                setFrame(thumb, c.id);
                const holder = ui.fitPreview(thumb); holder.classList.add('pw-thumb');
                const caption = node('span', undefined, 'pw-page-caption');
                caption.append(node('b', String(i + 1)), node('span', families[c.family] || c.family), node('small', c.id));
                pick.append(holder, caption);
                pick.addEventListener('click', () => select(c.id));
                item.append(pick);
                list.append(item);
            });
        }
        function renderStage() {
            const c = component(selected) || pkg.manifest.components[0];
            if (!c) return;
            selected = c.id;
            setFrame(stageFrame, c.id);
            pageLabel.replaceChildren(node('strong', families[c.family] || c.family), node('span', c.description), node('small', `正文块 ${c.blocks.minimum}–${c.blocks.maximum} · 指标 ${c.metrics.minimum}–${c.metrics.maximum} · 配图 ${c.images.minimum}–${c.images.maximum}`));
            if(c.reference_asset) pageLabel.append(ui.button('对照原稿',()=>{
                const asset=pkg.manifest.assets?.find(a=>a.id===c.reference_asset);
                if(!asset)return;
                const compare=ui.dialog(`原稿对照 · 第 ${c.source_slide || ''} 页`);
                const columns=node('div',undefined,'pptx-compare');
                const original=node('div');const image=node('img');image.src=`data:${asset.media_type};base64,${asset.data}`;image.alt='原稿页面';original.append(node('h3','原稿'),image);
                const current=node('div');const frame=node('iframe');frame.title='模板包样例';frame.setAttribute('sandbox','');setFrame(frame,c.id);current.append(node('h3','模板包样例'),ui.fitPreview(frame));
                columns.append(original,current);compare.append(columns,node('p','底图保留原稿装饰。请检查文字位置、字体替换及图片层级；发布后可替换槽位内容。'));
            }));
            const index = pkg.manifest.components.indexOf(c);
            if (!busy) {
                upBtn.disabled = index === 0;
                downBtn.disabled = index === pkg.manifest.components.length - 1;
                delBtn.disabled = pkg.manifest.components.length <= 1;
            }
            for (const item of list.children) item.querySelector('.pw-page-pick').setAttribute('aria-current', item.dataset.id === c.id ? 'true' : 'false');
        }
        function select(id) { selected = id; renderStage(); }
        function refresh() { renderHeader(); renderReferences(); renderList(); setBusy(busy); renderStage(); renderLog(); undoBtn.disabled=busy || !undo.length; }
        function apply(next) {
            pkg = next; live.clear();
            const idx = packages.findIndex(p => p.id === next.id);
            if (idx >= 0) packages[idx] = next; else packages.push(next);
            if (!component(selected)) selected = pkg.manifest.components[0]?.id;
            refresh();
        }
        function renderLog() {
            log.replaceChildren();
            if (!rounds.length) { log.append(node('li', '描述想要的调整，AI 会修改、删除或新增页面。每一轮的结果都会先显示在左侧预览中。', 'pw-log-empty')); return; }
            for (const r of rounds) {
                const item = node('li', undefined, 'pw-round' + (r.error ? ' is-error' : ''));
                item.append(node('p', r.prompt, 'pw-round-ask'));
                if (r.changes?.length) {
                    const ul = node('ul');
                    for (const ch of r.changes) ul.append(node('li', `${names[ch.action]} · ${ch.component_id}：${ch.description}`));
                    item.append(ul);
                }
                if (r.note) item.append(node('p', r.note, 'pw-round-note'));
                log.append(item);
            }
            log.scrollTop = log.scrollHeight;
        }
        async function ensureDraft() {
            if (pkg.editable) return pkg;
            const draft = await request(`${api}/${pkg.id}/draft`, 'POST');
            apply(draft);
            say(`已创建草稿 v${draft.version}，原版本保持不变。`);
            return draft;
        }
        async function run(task) {
            if (busy) return;
            setBusy(true);
            try { await task(); }
            catch (error) { if (error.name !== 'AbortError') say(error.message, true); }
            finally { setBusy(false); progress.hidden = true; controller = null; renderHeader(); renderStage(); }
        }
        async function change(labelText, perform) {
            await run(async () => {
                const draft = await ensureDraft();
                const before = draft.manifest;
                const next = await perform(draft);
                undo.push({manifest: before, label: labelText});
                apply(next);
                say(`${labelText}，已保存到草稿。`);
            });
        }
        function hashParam(draft) { return `expected_hash=${encodeURIComponent(draft.content_hash)}`; }
        function move(offset) {
            const id = selected;
            return change(offset < 0 ? '已上移' : '已下移', d => request(`${api}/${d.id}/components/${encodeURIComponent(id)}/move`, 'POST', {offset, expected_hash: d.content_hash}));
        }
        function duplicate() {
            const id = selected;
            return change('已复制页面', async d => {
                const before = new Set(d.manifest.components.map(c => c.id));
                const next = await request(`${api}/${d.id}/components/${encodeURIComponent(id)}/duplicate?${hashParam(d)}`, 'POST');
                selected = next.manifest.components.find(c => !before.has(c.id))?.id || selected;
                return next;
            });
        }
        async function removePage() {
            const c = component(selected);
            if (!c || !await ui.confirmDialog('删除页面', `从模板包中删除“${label(c)}”？可以用“撤销”恢复。`, '删除', true)) return;
            await change('已删除页面', d => request(`${api}/${d.id}/components/${encodeURIComponent(c.id)}?${hashParam(d)}`, 'DELETE'));
        }
        async function undoLast() {
            const last = undo[undo.length - 1];
            if (!last) return;
            await run(async () => {
                const next = await request(`${api}/${pkg.id}/manifest`, 'PUT', {manifest: last.manifest, expected_hash: pkg.content_hash});
                undo.pop(); apply(next);
                rounds.push({prompt: `撤销：${last.label}`, note: '已恢复到上一步。', done: true}); renderLog();
                say('已撤销上一步。');
            });
        }
        async function stream(url, payload, round) {
            const before = pkg;
            controller = new AbortController();
            progress.hidden = false; progress.removeAttribute('value');
            const response = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), signal: controller.signal});
            let result = null;
            try { await ui.readEditStream(response, async item => {
                if (item.type === 'progress') {
                    say(item.message || '');
                    if (item.total) { progress.max = item.total; progress.value = item.current; }
                    if (item.component_id && component(item.component_id)) select(item.component_id);
                } else if (item.type === 'component') {
                    // Live preview before the round is saved.
                    if (item.action === 'delete') {
                        list.querySelector(`[data-id="${CSS.escape(item.component_id)}"]`)?.classList.add('is-removed');
                    } else {
                        live.set(item.component_id, item.html);
                        let entry = list.querySelector(`[data-id="${CSS.escape(item.component_id)}"]`);
                        if (!entry) {
                            pkg = {...pkg, manifest: {...pkg.manifest, components: [...pkg.manifest.components, {id: item.component_id, family: item.family, description: item.description, blocks: {minimum: 0, maximum: 0}, metrics: {minimum: 0, maximum: 0}, images: {minimum: 0, maximum: 0}}]}};
                            renderList();
                            entry = list.querySelector(`[data-id="${CSS.escape(item.component_id)}"]`);
                        } else {
                            setFrame(entry.querySelector('iframe'), item.component_id);
                        }
                        entry?.classList.add('is-updated');
                        selected = item.component_id; setFrame(stageFrame, item.component_id);
                        for (const li of list.children) li.querySelector('.pw-page-pick').setAttribute('aria-current', li.dataset.id === selected ? 'true' : 'false');
                    }
                    if (round) { round.changes = [...(round.changes || []), {action: item.action, component_id: item.component_id, description: '已完成，等待保存'}]; renderLog(); }
                } else if (item.type === 'complete') {
                    result = item;
                }
            }); } catch (error) {
                // Streamed components are previews until the server confirms saving.
                pkg = before; live.clear();
                if (!component(selected)) selected = pkg.manifest.components[0]?.id;
                throw error;
            }
            return result;
        }
        async function aiRound() {
            if (busy) return;
            const prompt = chatInput.value.trim();
            if (!prompt) { say('请描述希望怎样修改模板包。', true); return; }
            const round = {prompt};
            rounds.push(round); renderLog();
            await run(async () => {
                try {
                    const draft = await ensureDraft();
                    const history = rounds.filter(r => r !== round && !r.error && r.done).map(r => r.prompt).slice(-8);
                    const result = await stream(`${api}/${draft.id}/edit`, {prompt, history, expected_hash: draft.content_hash}, round);
                    undo.push({manifest: draft.manifest, label: `AI：${prompt.slice(0, 24)}`});
                    round.changes = result.changes; round.done = true; round.note = '已保存到草稿。';
                    chatInput.value = '';
                    apply(result.package);
                    say(result.message || '已保存到草稿。');
                } catch (error) {
                    round.error = true; round.changes = []; round.note = error.name === 'AbortError' ? '已取消，重新打开可核对已保存的草稿。' : error.message;
                    live.clear(); refresh();
                    throw error;
                }
            });
        }
        async function aiPage() {
            if (busy) return;
            const instruction = pageInput.value.trim();
            if (!instruction) { say('请填写此页的修改要求。', true); return; }
            const id = selected;
            const round = {prompt: `第 ${pkg.manifest.components.findIndex(c => c.id === id) + 1} 页：${instruction}`};
            rounds.push(round); renderLog();
            await run(async () => {
                try {
                    const draft = await ensureDraft();
                    const result = await stream(`${api}/${draft.id}/components-ai`, {action: 'modify', component_id: id, instruction, expected_hash: draft.content_hash}, round);
                    undo.push({manifest: draft.manifest, label: `修改 ${id}`});
                    round.changes = result.changes; round.done = true; round.note = '已保存到草稿。';
                    pageInput.value = '';
                    apply(result.package); select(id);
                    say('此页已更新并保存到草稿。');
                } catch (error) {
                    round.error = true; round.changes = []; round.note = error.message;
                    live.clear(); refresh();
                    throw error;
                }
            });
        }
        async function aiAdd() {
            if (busy) return;
            const instruction = addText.value.trim();
            if (!instruction) { say('请描述新页面的用途和布局。', true); return; }
            const reference = addRef.value;
            const round = {prompt: `新增页面：${instruction}`};
            rounds.push(round); renderLog();
            await run(async () => {
                try {
                    const draft = await ensureDraft();
                    const result = await stream(`${api}/${draft.id}/components-ai`, {action: 'add', reference_id: reference, instruction, expected_hash: draft.content_hash}, round);
                    undo.push({manifest: draft.manifest, label: '新增页面'});
                    round.changes = result.changes; round.done = true; round.note = '已保存到草稿。';
                    addText.value = ''; addForm.hidden = true;
                    const added = result.changes[0]?.component_id;
                    apply(result.package); if (added) select(added);
                    say('新页面已添加到草稿。');
                } catch (error) {
                    round.error = true; round.changes = []; round.note = error.message;
                    live.clear(); refresh();
                    throw error;
                }
            });
        }
        async function rename() {
            if (busy) return;
            const input = node('input'); input.value = pkg.template_name; input.maxLength = 255; input.className = 'pw-rename'; input.setAttribute('aria-label', '模板包名称');
            title.replaceChild(input, nameEl); renameBtn.hidden = true; input.focus(); input.select();
            let done = false;
            const finish = async save => {
                if (done) return; done = true;
                const value = input.value.trim();
                title.replaceChild(nameEl, input); renameBtn.hidden = false;
                if (!save || !value || value === pkg.template_name) return;
                try {
                    await request(`/api/global-master-templates/package-templates/${pkg.template_id}`, 'PATCH', {name: value});
                    const fresh = await request(`${api}/${pkg.id}`);
                    apply(fresh); say('已重命名。');
                } catch (error) { say(error.message, true); }
            };
            input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); finish(true); } if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); } });
            input.addEventListener('blur', () => finish(true));
        }
        async function publish() {
            await run(async () => {
                say('正在校验全部页面…');
                const next = await request(`${api}/${pkg.id}/publish`, 'POST');
                apply(next); undo.length = 0;
                say(`v${next.version} 已发布，可以用于生成。继续修改会创建新草稿。`);
            });
        }

        refresh();
        if (pkg.editable) say('草稿中的修改会自动保存。');
    }

    window.PackageWorkspace = {open};
})();
