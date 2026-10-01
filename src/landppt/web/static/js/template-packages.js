(() => {
    'use strict';
    const api = '/api/global-master-templates/packages';
    const panel = document.getElementById('templatePackagePanel');
    const project = panel?.dataset.projectId || window.projectId || window.location.pathname.split('/')[2];
    const statuses = {draft:'草稿', validated:'已校验', published:'可用版本', retired:'已停用'};
    const families = {cover:'封面', section:'章节', points:'要点', comparison:'对比', process:'流程', image:'图文', metrics:'指标', summary:'总结'};
    let packages = [];
    let currentPage = 1;
    let pageSize = 6;
    async function request(url, method='GET', data) {
        const response = await fetch(url, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:data === undefined ? undefined : JSON.stringify(data)});
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body));
        return body;
    }
    function node(tag, text, className) {
        const el = document.createElement(tag);
        if (text !== undefined) el.textContent = text;
        if (className) el.className = className;
        return el;
    }
    function message(text, error=false) {
        const target = document.getElementById('packageStatus');
        if (target) { target.textContent = text; target.classList.toggle('error', error); }
    }
    function button(text, action, primary=false) {
        const el = node('button', text, primary ? 'primary' : '');
        el.type = 'button';
        bindAction(el, action);
        return el;
    }
    function bindAction(el, action) {
        el.addEventListener('click', async () => {
            el.disabled = true;
            try { await action(); } catch(error) { message(error.message, true); const status = document.querySelector('.package-dialog[open] .package-status'); if(status) status.textContent = error.message; }
            finally { el.disabled = false; }
        });
    }
    function dialog(title) {
        const modal = node('dialog', undefined, 'package-dialog');
        const head = node('header'); head.append(node('h2', title), button('关闭', () => modal.close()));
        modal.append(head); document.body.append(modal);
        modal.addEventListener('close', () => modal.remove());
        modal.showModal();
        return modal;
    }
    function fitPreview(frame) {
        const holder = node('div', undefined, 'package-preview-holder');
        holder.append(frame);
        const observer = new ResizeObserver(() => {
            frame.style.transform = `scale(${holder.clientWidth / 1280})`;
        });
        observer.observe(holder);
        holder.disposePreview = () => observer.disconnect();
        const modal = document.querySelector('.package-dialog[open]');
        if (modal) modal.addEventListener('close', () => observer.disconnect(), {once: true});
        return holder;
    }
    function preview(pkg) {
        const modal = dialog(pkg.template_name + ' · v' + pkg.version);
        const select = node('select'); select.setAttribute('aria-label','预览版式');
        for (const c of pkg.manifest.components) { const option = node('option', (families[c.family] || c.family) + ' · ' + c.description); option.value=c.id; select.append(option); }
        const frame = node('iframe', undefined, 'preview-frame'); frame.title='模板包版式预览'; frame.setAttribute('sandbox','');
        function update() { frame.src = `${api}/${pkg.id}/preview/${encodeURIComponent(select.value)}`; }
        select.addEventListener('change',update); modal.append(select,fitPreview(frame),node('p','预览使用示例内容；图文版式中的灰色区域为配图位置。')); update();
    }
    async function load() {
        packages = (await request(api)).packages;
        render();
    }
    function confirmDialog(title, text, okText='确定', danger=false) {
        return new Promise(resolve => {
            const modal=dialog(title); modal.classList.add('package-confirm');
            let result=false;
            const ok=button(okText,()=>{result=true;modal.close();},true); if(danger) ok.classList.add('danger');
            const footer=node('footer'); footer.append(button('取消',()=>modal.close()),ok);
            modal.append(node('p',text),footer);
            modal.addEventListener('close',()=>resolve(result),{once:true});
        });
    }
    function promptDialog(title, label, value, {multiline=false, maxLength=255, allowEmpty=false, onSave=async()=>{}} = {}) {
        return new Promise(resolve => {
            const modal=dialog(title); modal.classList.add('package-confirm');
            const wrap=node('label',label); const input=node(multiline?'textarea':'input'); input.value=value || ''; input.maxLength=maxLength; input.required=!allowEmpty; wrap.append(input);
            if(multiline) { input.rows=5; input.placeholder='介绍模板包的用途、风格和适用场景（可留空）'; }
            const status=node('div','','package-status'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
            let result=null;
            let saving=false;
            const form=node('form'); form.addEventListener('submit',async e=>{
                e.preventDefault(); if(saving) return;
                const next=input.value.trim();
                if(!allowEmpty && !next) { input.setCustomValidity('请填写'+label); input.reportValidity(); return; }
                saving=true; input.disabled=true; modal.querySelectorAll('button').forEach(b=>b.disabled=true);
                try { await onSave(next); result=next; modal.close(); }
                catch(error) { status.textContent=error.message; status.classList.add('error'); }
                finally { saving=false; input.disabled=false; modal.querySelectorAll('button').forEach(b=>b.disabled=false); }
            });
            input.addEventListener('input',()=>input.setCustomValidity(''));
            modal.addEventListener('cancel',e=>{if(saving)e.preventDefault();});
            const footer=node('footer'); const save=node('button','保存','primary'); save.type='submit';
            footer.append(button('取消',()=>modal.close()),save); form.append(wrap,status,footer); modal.append(form);
            modal.addEventListener('close',()=>resolve(result),{once:true});
            input.focus(); input.select();
        });
    }
    async function download(pkg) {
        const response=await fetch(`${api}/${pkg.id}/export`,{credentials:'same-origin'});
        if(!response.ok) throw new Error('模板包导出失败');
        const url=URL.createObjectURL(await response.blob());
        const extension=response.headers.get('Content-Type')?.includes('zip')?'zip':'json';
        const a=node('a'); a.href=url; a.download=`${pkg.manifest.package_id}-v${pkg.version}.${extension}`;
        (Array.from(document.querySelectorAll('dialog[open]')).at(-1) || document.body).append(a);
        a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    async function renamePackage(pkg) {
        const name=await promptDialog('重命名模板包','名称',pkg.template_name);
        if(!name || name===pkg.template_name) return;
        await request(`/api/global-master-templates/package-templates/${pkg.template_id}`,'PATCH',{name});
        await load(); message('已重命名。已发布版本的内容不变。');
    }
    async function editDescription(pkg) {
        const current=pkg.description ?? pkg.manifest.description;
        const value=await promptDialog('编辑模板包描述','描述',current,{
            multiline:true,maxLength:2000,allowEmpty:true,
            onSave:description=>description===current ? Promise.resolve() : request(`/api/global-master-templates/package-templates/${pkg.template_id}`,'PATCH',{description})
        });
        return value!==null && value!==current;
    }
    async function deletePackage(pkg) {
        const versions=packages.filter(p=>p.template_id===pkg.template_id).length;
        if(!await confirmDialog('删除模板包',`将删除“${pkg.template_name}”及其全部 ${versions} 个版本。正在使用它的项目不受影响，但之后不能再选择它。`,'删除模板包',true)) return;
        const result=await request(`/api/global-master-templates/package-templates/${pkg.template_id}`,'DELETE');
        await load(); message(result.hidden?`已删除。${result.projects} 个项目仍在使用旧版本，这些项目可以继续编辑和导出。`:'模板包已删除。');
    }
    async function deleteVersion(pkg) {
        if(!await confirmDialog('删除草稿',`删除“${pkg.template_name}”的草稿 v${pkg.version}？此操作不可撤销。`,'删除草稿',true)) return;
        await request(`${api}/${pkg.id}`,'DELETE'); await load(); message('草稿已删除。');
    }
    function openWorkspace(pkg) {
        if(!window.PackageWorkspace) throw new Error('编辑器加载失败，请刷新页面后重试。');
        window.PackageWorkspace.open(pkg,{packages,onChange:()=>load().catch(e=>message(e.message,true))});
    }
    function renderPagination(total) {
        const pagination = document.getElementById('packagePagination');
        const pages = Math.max(1, Math.ceil(total / pageSize));
        currentPage = Math.min(currentPage, pages);
        pagination.hidden = total === 0;
        document.getElementById('packagePageInfo').textContent = total
            ? `${(currentPage - 1) * pageSize + 1}–${Math.min(currentPage * pageSize, total)} / 共 ${total} 个版本 · 第 ${currentPage}/${pages} 页`
            : '共 0 个版本';
        const controls = document.getElementById('packagePageButtons');
        controls.replaceChildren();
        function pageButton(label, target, disabled=false) {
            const el = node('button', label);
            el.type = 'button'; el.disabled = disabled;
            if (typeof label === 'number') {
                el.setAttribute('aria-label', `第 ${target} 页`);
                if (target === currentPage) el.setAttribute('aria-current', 'page');
            }
            el.addEventListener('click', () => {
                currentPage = target;
                render();
                // Start reading the new cards; keep keyboard focus in the list.
                const grid = document.getElementById('packageGrid');
                grid.tabIndex = -1;
                grid.focus({preventScroll:true});
                panel.scrollIntoView({block:'start'});
            });
            controls.append(el);
        }
        pageButton('上一页', currentPage - 1, currentPage === 1);
        const start = Math.max(1, Math.min(currentPage - 2, pages - 4));
        const numbers = [...new Set([1, ...Array.from({length:Math.min(5,pages)}, (_, i) => start + i), pages])].sort((a,b)=>a-b);
        let previous = 0;
        for (const value of numbers) {
            if (previous && value - previous > 1) controls.append(node('span', '…'));
            pageButton(value, value);
            previous = value;
        }
        pageButton('下一页', currentPage + 1, currentPage === pages);
    }
    function render() {
        const grid = document.getElementById('packageGrid');
        grid.querySelectorAll('.package-preview-holder').forEach(el=>el.disposePreview?.());
        grid.replaceChildren();
        const filter = document.getElementById('packageFilter').value;
        const preferred = Number(panel.dataset.preferredVersion || 0);
        const visible = packages.filter(p => filter === 'all' || p.status === filter)
            .sort((a, b) => (b.id === preferred) - (a.id === preferred));
        renderPagination(visible.length);
        if (!visible.length) grid.append(node('p',packages.length ? '没有符合条件的模板包版本，请切换筛选条件。' : '还没有模板包。添加内置包，或用 AI 创建自己的设计。','package-empty'));
        for (const pkg of visible.slice((currentPage - 1) * pageSize, currentPage * pageSize)) {
            const card = node('article',undefined,'package-card');
            const cover = node('div',undefined,'package-card-preview'); const frame=node('iframe');
            frame.src=`${api}/${pkg.id}/preview/${encodeURIComponent(pkg.manifest.components[0].id)}?h=${pkg.content_hash.slice(0,12)}`; frame.title=pkg.template_name+'预览'; frame.loading='lazy'; frame.setAttribute('sandbox',''); frame.tabIndex=-1; cover.append(fitPreview(frame));
            const body=node('div',undefined,'package-card-body');
            const meta=node('small',`v${pkg.version} · ${pkg.manifest.components.length} 个页面`);
            const badge=node('span',statuses[pkg.status],'package-badge is-'+pkg.status);
            const title=node('div',undefined,'package-card-title'); title.append(node('h3',pkg.template_name),badge);
            body.append(title,meta,node('p',pkg.description ?? pkg.manifest.description,'package-description'));
            if(pkg.id===preferred){card.classList.add('is-preferred');body.append(node('p','需求确认时已选择，大纲已按此模板包的版式规划','package-preferred-note'));}
            const actions=node('div',undefined,'package-tools package-card-actions');
            if(panel.dataset.mode==='select' && pkg.status==='published') actions.append(button('使用此模板包快速生成', async()=>{
                await request(`/api/projects/${encodeURIComponent(project)}/template-package`,'POST',{version_id:pkg.id,options:{allow_images:document.getElementById('packageImages').checked,image_budget:Number(document.getElementById('packageBudget').value)}});
                window.location.href=typeof getPPTGeneratorUrl==='function' ? getPPTGeneratorUrl() : `/projects/${encodeURIComponent(project)}/slides`;
            },true));
            if(pkg.editable) actions.append(button('校验并发布',async()=>{await request(`${api}/${pkg.id}/publish`,'POST');await load();message('版本已发布，可以用于生成。');},true));
            const row=node('div',undefined,'package-card-row');
            row.append(button(pkg.editable?'编辑草稿':'编辑',()=>openWorkspace(pkg)),button('预览',()=>preview(pkg)));
            const menu=node('details',undefined,'package-menu');
            const summary=node('summary','更多'); summary.setAttribute('aria-label','更多操作');
            const list=node('div',undefined,'package-menu-list');
            if(pkg.user_id) list.append(button('重命名',()=>renamePackage(pkg)),button('编辑描述',async()=>{if(await editDescription(pkg)){await load();message('描述已保存。');}}));
            list.append(button('导出 JSON',()=>download(pkg)));
            list.append(button('复制为新模板包',async()=>{await request(api,'POST',{manifest:{...pkg.manifest,name:pkg.template_name,description:pkg.description ?? pkg.manifest.description}});await load();message('已复制为新的模板包草稿。');}));
            if(pkg.user_id && pkg.status==='published') list.append(button('停用此版本',async()=>{await request(`${api}/${pkg.id}/retire`,'POST');await load();message('已停用。引用此版本的项目仍可继续使用。');}));
            if(pkg.editable) list.append(button('删除此草稿',()=>deleteVersion(pkg)));
            if(pkg.user_id){const del=button('删除模板包',()=>deletePackage(pkg)); del.classList.add('danger'); list.append(del);}
            list.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>menu.open=false));
            menu.append(summary,list); row.append(menu);
            actions.append(row);
            body.append(actions);card.append(cover,body);grid.append(card);
        }
    }
    async function readEditStream(response, onEvent) {
        if (!response.ok) throw new Error((await response.json()).detail || '编辑失败');
        if (!response.body) throw new Error('浏览器无法读取生成进度，请刷新后重试。');
        const reader=response.body.getReader(), decoder=new TextDecoder();
        let buffer='';
        try {
            while (true) {
                const {done,value}=await reader.read();
                buffer+=decoder.decode(value || new Uint8Array(),{stream:!done}).replace(/\r\n/g,'\n');
                let split;
                while ((split=buffer.indexOf('\n\n'))>=0) {
                    const chunk=buffer.slice(0,split);buffer=buffer.slice(split+2);
                    const data=chunk.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
                    if (!data) continue;
                    const item=JSON.parse(data);
                    if (item.type==='error') throw new Error(item.message || '编辑失败');
                    await onEvent(item);
                    if (item.type==='complete') return;
                }
                if (done) throw new Error('连接已中断，未收到完成确认。请关闭窗口检查草稿列表后再重试。');
            }
        } finally { await reader.cancel().catch(()=>{}); reader.releaseLock(); }
    }
    async function generate() {
        const modal=dialog('创建自己的模板包');
        const instructions=node('textarea'); instructions.placeholder='例如：面向科研汇报，冷灰与墨蓝配色，突出图表、结论和研究过程。'; instructions.setAttribute('aria-label','模板包设计要求');
        const progress=node('progress',undefined,'package-progress');progress.max=10;progress.value=0;
        const status=node('div','生成完成后会保存为草稿。','package-status');status.setAttribute('role','status');
        const submit=button('开始设计',async()=>{
            if(!instructions.value.trim()) throw new Error('请描述希望的风格与用途。');
            status.textContent='正在规划主题…';
            const response=await fetch('/api/global-master-templates/packages-generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:instructions.value})});
            if(!response.ok) throw new Error((await response.json()).detail || '创建失败');
            const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
            while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let split;while((split=buffer.indexOf('\n\n'))>=0){const chunk=buffer.slice(0,split);buffer=buffer.slice(split+2);if(!chunk.startsWith('data: '))continue;const item=JSON.parse(chunk.slice(6));status.textContent=item.message || '';if(item.total){progress.max=item.total;progress.value=item.current;}if(item.type==='error')throw new Error(item.message);if(item.type==='complete'){progress.value=progress.max;status.textContent=item.partial?'部分版式未通过校验，已保存可用部分为草稿。请预览后决定是否发布。':'草稿已保存。关闭此窗口即可预览并发布。';await load();}}}
        },true);
        modal.append(instructions,progress,status,submit);
    }
    window.PackageUI={request,node,button,dialog,fitPreview,readEditStream,confirmDialog,download,editDescription,families,api};
    document.addEventListener('click',e=>{document.querySelectorAll('.package-menu[open]').forEach(m=>{if(!m.contains(e.target))m.open=false;});});
    if(panel){
        document.getElementById('packageInstall').addEventListener('click',async function(){this.disabled=true;try{await request(api+'/builtin','POST');await load();message('内置模板包已添加。');}catch(e){message(e.message,true);}finally{this.disabled=false;}});
        document.getElementById('packageCreate').addEventListener('click',generate);
        document.getElementById('packageFilter').addEventListener('change',()=>{currentPage=1;render();});
        document.getElementById('packagePageSize').addEventListener('change',e=>{pageSize=Number(e.target.value);currentPage=1;render();});
        document.getElementById('packageImport').addEventListener('change',async(e)=>{try{
            const file=e.target.files[0];if(!file)return;
            if(file.name.toLowerCase().endsWith('.zip')){
                if(file.size>65*1024*1024)throw new Error('模板包 ZIP 不能超过 65 MB');
                const data=new FormData();data.append('file',file);
                const response=await fetch('/api/global-master-templates/packages-import',{method:'POST',body:data,credentials:'same-origin'});
                const result=await response.json();if(!response.ok)throw new Error(result.detail || '导入失败');
            }else{
                if(file.size>5*1024*1024)throw new Error('JSON 文件不能超过 5 MB；含底图的模板包请使用 ZIP');
                await request(api,'POST',{manifest:JSON.parse(await file.text())});
            }
            await load();message('导入草稿成功，请预览并发布。');
        }catch(error){message(error.message,true);}finally{e.target.value='';}});
        document.getElementById('packagePptxImport')?.addEventListener('click',()=>window.PackagePptxImport.open({onComplete:async pkg=>{await load();openWorkspace(pkg);}}));
        load().catch(e=>message(e.message,true));
    }
    // The editor uses the same structured content endpoint; no HTML string interpolation.
    const editorTrigger = document.getElementById('packageContentEditorBtn');
    if(!panel && window.projectId && editorTrigger){
        request(`/api/projects/${project}/template-package`).then(()=>{
            bindAction(editorTrigger, openEditor);
            editorTrigger.hidden = false;
        }).catch(()=>{});
    }
    async function openEditor(){
        let snapshot=await request(`/api/projects/${project}/template-package`);
        const index=typeof currentSlideIndex==='number'?currentSlideIndex:0;
        let page=snapshot.pages.find(p=>p.slide_index===index);
        if(!page)throw new Error('此页没有模板包内容稿。');
        const modal=dialog('内容与版式 · 第 '+(index+1)+' 页');
        const status=node('div',page.manual?'此页已自由编辑。恢复模板包控制后，可编辑内容稿。':(page.error || ''),'package-status');status.setAttribute('role','status');modal.append(status);
        if(!page.content){status.textContent='页面内容尚未生成，请先生成页面。';return;}
        const content=structuredClone(page.content), fields=node('div',undefined,'content-fields');
        function field(label,obj,key,multiline=true){const wrap=node('label',label);const input=node(multiline?'textarea':'input');input.value=obj[key] || '';input.addEventListener('input',()=>obj[key]=input.value);wrap.append(input);fields.append(wrap);}
        field('标题',content,'title',false);field('副标题',content,'subtitle');
        content.blocks.forEach((block,i)=>{field(`要点 ${i+1} · 小标题`,block,'heading',false);field(`要点 ${i+1} · 正文`,block,'body');});
        content.metrics.forEach((metric,i)=>{field(`指标 ${i+1} · 名称`,metric,'label',false);field('数值',metric,'value',false);field('单位',metric,'unit',false);});field('结论',content,'takeaway');
        const layouts=await request(`/api/projects/${project}/package-pages/${page.slide_id}/layouts`);const select=node('select');select.setAttribute('aria-label','页面版式');for(const c of layouts.components){const option=node('option',(families[c.family]||c.family)+' · '+c.description);option.value=c.id;select.append(option);}select.value=page.component_id || select.value;
        async function action(payload){await request(`/api/projects/${project}/package-pages/${page.slide_id}/action`,'POST',{expected_revision:page.revision,...payload});window.location.reload();}
        const footer=node('footer');footer.append(select,button('应用版式',()=>action({component_id:select.value}),true));
        footer.append(button('保存内容',async()=>{await request(`/api/projects/${project}/package-pages/${page.slide_id}/content`,'PUT',{expected_revision:page.revision,content});window.location.reload();},true));
        footer.append(button(page.manual?'恢复模板包控制':'转为自由编辑',()=>action({action:page.manual?'restore':'manual',component_id:select.value})));
        footer.append(button(page.locked?'解锁页面':'锁定页面',()=>action({action:page.locked?'unlock':'lock'})));
        const ai=node('textarea');ai.placeholder='例如：保留所有事实，将结论改写得更简洁。';ai.setAttribute('aria-label','内容修改要求');
        const aiButton=button('按要求修改内容',async()=>{if(!ai.value.trim())return;await request(`/api/projects/${project}/package-pages/${page.slide_id}/edit`,'POST',{expected_revision:page.revision,instruction:ai.value});window.location.reload();});
        modal.append(fields,footer,ai,aiButton);
        for(const visual of content.visual_briefs){const label=node('label','更换配图 · '+visual.brief);const input=node('input');input.placeholder='从图片库复制图片 ID';input.setAttribute('aria-label','替换图片 ID');label.append(input,button('替换图片',()=>action({action:'image',visual_id:visual.id,image_id:input.value,component_id:select.value})));modal.append(label);}
    }
})();
