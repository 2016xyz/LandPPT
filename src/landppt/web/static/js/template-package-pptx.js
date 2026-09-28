(() => {
    'use strict';
    window.PackagePptxImport = {open};
    function open({onComplete}) {
        const {node, dialog, button, families} = window.PackageUI;
        const modal = dialog('从 PPTX 创建模板包');
        modal.classList.add('pptx-import');
        const intro = node('p', '参考原稿生成可复用模板，确认内容用途后保存草稿，再对照原页检查效果。', 'pptx-intro');
        const fileLabel = node('label', '本地演示文件');
        const fileInput = node('input'); fileInput.type='file'; fileInput.accept='.pptx,.potx,.ppsx';
        fileLabel.append(fileInput);
        const nameLabel = node('label','模板包名称'); const name=node('input');name.maxLength=255;name.required=true;nameLabel.append(name);
        const top = node('div',undefined,'pptx-controls');top.append(fileLabel,nameLabel);
        const modeLabel=node('label','导入方式');modeLabel.className='pptx-mode';
        const mode=node('select');mode.setAttribute('aria-label','导入方式');
        for(const [value,label] of [['visual','视觉复刻（推荐）'],['preserve','保留原稿底图']]){const option=node('option',label);option.value=value;mode.append(option);}
        const modeHint=node('small','AI 参考页面截图重建可编辑版式，允许调整拥挤布局、简化复杂装饰。需要支持图片输入的模板生成模型。');modeLabel.append(mode,modeHint);top.append(modeLabel);
        const status=node('p','支持 PPTX / POTX / PPSX，最大 50 MB。每次最多选择 40 页。','package-status');status.setAttribute('role','status');
        const warnings=node('div',undefined,'pptx-warnings');
        const stages=node('ol',undefined,'pptx-stages');stages.hidden=true;
        const pages=node('div',undefined,'pptx-pages');
        const analyze=button('分析页面',runAnalyze,true);
        const create=button('生成模板包草稿',runImport,true);create.hidden=true;
        const activity=node('progress');activity.className='pptx-activity';activity.hidden=true;activity.setAttribute('aria-label','模板包处理进行中');
        const footer=node('footer');footer.className='pptx-import-footer';footer.append(status,activity,analyze,create);
        modal.append(intro,top,stages,warnings,pages,footer);
        let rows=[], file=null, busy=false;
        let timer=null,started=0,lastMessage='';
        function setBusy(value,text){busy=value;status.textContent=text;activity.hidden=!value;modal.setAttribute('aria-busy',String(value));for(const el of modal.querySelectorAll('input,select,footer button'))el.disabled=value;if(!value){analyze.textContent='分析页面';create.textContent='生成模板包草稿';}}
        function progress(stage,message){
            lastMessage=message;
            status.textContent=`${message} · 已用时 ${Math.floor((Date.now()-started)/1000)} 秒`;
            let active=false;
            for(const li of stages.children){
                if(li.dataset.stage===stage){active=true;li.setAttribute('aria-current','step');li.dataset.state='active';}
                else if(!active && stages.querySelector(`[data-stage="${stage}"]`)){li.dataset.state='done';li.removeAttribute('aria-current');}
                else {li.dataset.state='pending';li.removeAttribute('aria-current');}
            }
        }
        function showWarnings(items){warnings.replaceChildren();if(!items.length)return;const list=node('ul');items.forEach(s=>list.append(node('li',s)));warnings.append(list);}
        async function post(action,data){
            const steps=action==='analyze'?[['queued','上传文件'],['parse','解析文件'],['render','渲染原稿'],['extract','提取内容'],['ai',mode.value==='visual'?'视觉识别':'AI 识别'],['repair','AI 修复']]:[['queued','上传文件'],['parse','解析文件'],['render','渲染原稿'],['bind','绑定槽位'],...(mode.value==='visual'?[['vision','视觉复刻']]:[['background','生成底图']]),['validate','校验版式'],['repair','AI 修复'],['save','保存草稿']];
            stages.replaceChildren(...steps.map(([key,label])=>{const li=node('li',label);li.dataset.stage=key;return li;}));stages.hidden=false;
            started=Date.now();progress('queued','正在上传文件');
            timer=setInterval(()=>{if(busy)status.textContent=`${lastMessage} · 已用时 ${Math.floor((Date.now()-started)/1000)} 秒`;},1000);
            try {
                const response=await fetch('/api/global-master-templates/packages-pptx/'+action+'?stream=true&vision='+(mode.value==='visual'),{method:'POST',body:data,credentials:'same-origin'});
                if(!response.ok){const result=await response.json();throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));}
                if(!response.body)throw new Error('浏览器无法读取进度，请刷新后重试');
                const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',finished=false;
                try {
                    while(true){
                        const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done});
                        const lines=buffer.split('\n');buffer=lines.pop();if(done&&buffer.trim()){lines.push(buffer);buffer='';}
                        for(const line of lines){
                            if(!line.trim())continue;const event=JSON.parse(line);
                            if(event.type==='progress')progress(event.stage,event.message);
                            if(event.type==='error')throw new Error(event.message);
                            if(event.type==='complete'){finished=true;for(const li of stages.children){li.dataset.state='done';li.removeAttribute('aria-current');}return event.result;}
                        }
                        if(done)throw new Error('连接已中断，尚未收到完成结果。请重试。');
                    }
                }finally{if(!finished)await reader.cancel().catch(()=>{});reader.releaseLock();}
            }finally{clearInterval(timer);timer=null;}
        }
        fileInput.addEventListener('change',()=>{
            file=fileInput.files[0];name.value=file?.name.replace(/\.[^.]+$/,'') || '';
            rows=[];pages.replaceChildren();warnings.replaceChildren();create.hidden=true;
        });
        mode.addEventListener('change',()=>{
            rows=[];pages.replaceChildren();warnings.replaceChildren();create.hidden=true;stages.hidden=true;
            modeHint.textContent=mode.value==='visual'?'AI 参考页面截图重建可编辑版式，允许调整拥挤布局、简化复杂装饰。需要支持图片输入的模板生成模型。':'保留复杂装饰作为底图，仅替换已确认槽位。较小的文字区域可能无法容纳新内容。';
            status.textContent='导入方式已切换，请重新分析页面。';
        });
        async function runAnalyze(){
            if(busy)return;
            if(!file){status.textContent='请先选择本地 PPTX 文件。';return;}
            if(file.size>50*1024*1024){status.textContent='文件不能超过 50 MB。';return;}
            setBusy(true,'正在渲染原稿并识别文字框，请稍候…');
            analyze.textContent='正在分析…';
            try{
                const data=new FormData();data.append('file',file);
                const result=await post('analyze',data);
                rows=[];pages.replaceChildren();showWarnings(result.warnings);
                result.slides.forEach(slide=>{
                    const card=node('details',undefined,'pptx-page');
                    const summary=node('summary');
                    const pick=node('input');pick.type='checkbox';pick.checked=!slide.review_required && slide.slide<=40 && slide.candidates.some(c=>c.suggested==='title');pick.setAttribute('aria-label',`选择第 ${slide.slide} 页`);pick.addEventListener('click',e=>e.stopPropagation());
                    const thumbnail=node('img');thumbnail.src=slide.preview;thumbnail.alt=`原稿第 ${slide.slide} 页`;thumbnail.loading='lazy';
                    summary.append(pick,thumbnail,node('strong',`第 ${slide.slide} 页${slide.hidden?' · 隐藏页':''}${slide.review_required?' · 需调整':''}`),node('span',`${slide.candidates.length} 个候选内容框`));card.append(summary);
                    const familyLabel=node('label','版式用途');const family=node('select');
                    Object.entries(families).forEach(([key,value])=>{const option=node('option',value);option.value=key;family.append(option);});
                    family.value=slide.family || (slide.slide===1?'cover':'points');familyLabel.append(family);card.append(familyLabel);
                    const table=node('div',undefined,'pptx-bindings');
                    const bindings={},groups={};
                    slide.candidates.forEach(candidate=>{
                        const line=node('label',undefined,'pptx-binding');
                        const label=node('span',candidate.text);label.title=candidate.text;if(candidate.kind==='text'&&candidate.capacity!==undefined){const cap=node('small',`约 ${candidate.capacity} 字`,'pptx-capacity'+(candidate.capacity<8?' is-small':''));cap.title='按最小可读字号估算，生成内容不能超过此字数';label.append(' ',cap);}
                        const role=node('select');role.setAttribute('aria-label',`${candidate.text.slice(0,30)}的用途`);
                        const fixedLabel=mode.value==='visual'?'装饰参考（不绑定）':'固定在底图';
                        const options=candidate.kind==='image'?{fixed:fixedLabel,image:'可替换配图',remove:'清除样例图片'}:{fixed:fixedLabel,remove:'清除样例文字',title:'标题（每页一个）',subtitle:'副标题',heading:'要点小标题',body:'正文 / 要点'};
                        Object.entries(options).forEach(([key,value])=>{const option=node('option',value);option.value=key;role.append(option);});role.value=candidate.suggested;
                        bindings[candidate.id]=role;
                        if(candidate.reason){label.append(node('small',`AI：${candidate.reason}`,'pptx-ai-reason'));}
                        line.append(label,role);table.append(line);
                        if(candidate.kind==='text'){
                            const group=node('input');group.value=candidate.group || '';group.placeholder='同一要点填同一分组';group.maxLength=80;group.setAttribute('aria-label',`${candidate.text.slice(0,20)}的要点分组`);groups[candidate.id]=group;
                            const toggle=()=>{group.hidden=!['heading','body'].includes(role.value);};role.addEventListener('change',toggle);toggle();line.append(group);
                        }
                    });
                    card.append(table);
                    slide.warnings.forEach(w=>card.append(node('p',w,'pptx-page-warning')));
                    rows.push({slide:slide.slide,pick,family,bindings,groups});pages.append(card);
                });
                create.hidden=false;
                setBusy(false,mode.value==='visual'?'视觉识别完成。请确认需要复刻的标题、正文和配图；装饰参考不绑定原主题文字。':'AI 识别完成。请展开检查用途和要点分组；“固定”会保留原文，“清除”会移除原主题文字。');
            }catch(error){setBusy(false,error.message);}
        }
        async function runImport(){
            if(busy)return;
            const selected=rows.filter(r=>r.pick.checked);
            if(!name.value.trim() || !selected.length || selected.length>40){status.textContent='请填写名称并选择 1–40 页。';return;}
            const choices=selected.map(r=>({slide:r.slide,family:r.family.value,bindings:Object.fromEntries(Object.entries(r.bindings).map(([id,select])=>[id,select.value])),groups:Object.fromEntries(Object.entries(r.groups).filter(([id,input])=>input.value.trim() && ['body','heading'].includes(r.bindings[id].value)).map(([id,input])=>[id,input.value.trim()]))}));
            if(choices.some(c=>Object.values(c.bindings).filter(v=>v==='title').length!==1)){status.textContent='每个选中页面必须指定且仅指定一个标题。';return;}
            setBusy(true,'正在生成底图、绑定内容并校验版式，请稍候…');
            create.textContent='正在生成…';
            try{
                const data=new FormData();data.append('file',file);data.append('options',JSON.stringify({name:name.value.trim(),pages:choices,mode:mode.value}));
                const result=await post('import',data);
                showWarnings([...result.warnings,...result.failures.map(f=>`第 ${f.slide} 页未导入：${f.error}`)]);
                setBusy(false,`已保存 ${result.package.manifest.components.length} 个版式到草稿${result.partial?'，部分页面未通过校验':''}。请对照原稿检查后发布。`);
                create.hidden=true;
                const review=button('打开草稿，对照原稿',async()=>{modal.close();await onComplete(result.package);},true);footer.append(review);
            }catch(error){setBusy(false,error.message);}
        }
    }
})();
