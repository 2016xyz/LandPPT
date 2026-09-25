(() => {
    'use strict';
    const api = '/api/global-master-templates/packages';
    const panel = document.getElementById('templatePackagePanel');
    const project = panel?.dataset.projectId || window.projectId || window.location.pathname.split('/')[2];
    const statuses = {draft:'草稿', validated:'已校验', published:'可用版本', retired:'已停用'};
    const families = {cover:'封面', section:'章节', points:'要点', comparison:'对比', process:'流程', image:'图文', metrics:'指标', summary:'总结'};
    let packages = [];
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
    function render() {
        const grid = document.getElementById('packageGrid'); grid.replaceChildren();
        const filter = document.getElementById('packageFilter').value;
        const visible = packages.filter(p => filter === 'all' || p.status === filter);
        if (!visible.length) grid.append(node('p','还没有模板包。添加内置包，或用 AI 创建自己的设计。','package-empty'));
        for (const pkg of visible) {
            const card = node('article',undefined,'package-card');
            const cover = node('div',undefined,'package-card-preview'); const frame=node('iframe');
            frame.src=`${api}/${pkg.id}/preview/${pkg.manifest.components[0].id}`; frame.title=pkg.template_name+'预览'; frame.loading='lazy'; frame.setAttribute('sandbox',''); frame.tabIndex=-1; cover.append(fitPreview(frame));
            const body=node('div',undefined,'package-card-body'); body.append(node('h3',pkg.template_name),node('small',`v${pkg.version} · ${statuses[pkg.status]} · ${pkg.manifest.components.length} 种版式`),node('p',pkg.manifest.description));
            const actions=node('div',undefined,'package-tools package-card-actions'); actions.append(button('预览版式',()=>preview(pkg)));
            const management=node('details',undefined,'package-card-management');
            management.append(node('summary','版本与管理'));
            const secondary=node('div',undefined,'package-tools');
            if(panel.dataset.mode==='select' && pkg.status==='published') actions.append(button('使用此模板包快速生成', async()=>{
                await request(`/api/projects/${encodeURIComponent(project)}/template-package`,'POST',{version_id:pkg.id,options:{allow_images:document.getElementById('packageImages').checked,image_budget:Number(document.getElementById('packageBudget').value)}});
                window.location.href=typeof getPPTGeneratorUrl==='function' ? getPPTGeneratorUrl() : `/projects/${encodeURIComponent(project)}/slides`;
            },true));
            if(pkg.user_id && ['draft','validated'].includes(pkg.status)) actions.append(button('校验并发布',async()=>{await request(`${api}/${pkg.id}/publish`,'POST');await load();message('版本已发布，可以用于生成。');},true));
            if(pkg.user_id && pkg.status==='published') secondary.append(button('停用',async()=>{await request(`${api}/${pkg.id}/retire`,'POST');await load();message('已停用。引用此版本的项目仍可继续使用。');}));
            secondary.append(button('复制为新草稿',async()=>{await request(api,'POST',{manifest:pkg.manifest});await load();message('草稿已保存。');}));
            if(pkg.user_id) secondary.append(button('创建下一版本',async()=>{await request(api,'POST',{manifest:pkg.manifest,template_id:pkg.template_id});await load();message('新版本草稿已保存，已有项目仍使用原版本。');}));
            secondary.append(button('导出模板包',()=>{const blob=new Blob([JSON.stringify(pkg.manifest,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=node('a');a.href=url;a.download=`${pkg.manifest.package_id}-v${pkg.version}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
            management.append(secondary);body.append(actions,management);card.append(cover,body);grid.append(card);
        }
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
    if(panel){
        document.getElementById('packageInstall').addEventListener('click',async function(){this.disabled=true;try{await request(api+'/builtin','POST');await load();message('内置模板包已添加。');}catch(e){message(e.message,true);}finally{this.disabled=false;}});
        document.getElementById('packageCreate').addEventListener('click',generate);
        document.getElementById('packageFilter').addEventListener('change',render);
        document.getElementById('packageImport').addEventListener('change',async(e)=>{try{const file=e.target.files[0];if(!file)return;if(file.size>5*1024*1024)throw new Error('模板包文件不能超过 5 MB');await request(api,'POST',{manifest:JSON.parse(await file.text())});await load();message('导入草稿成功，请预览并发布。');}catch(error){message(error.message,true);}finally{e.target.value='';}});
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
