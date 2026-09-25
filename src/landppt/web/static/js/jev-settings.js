(() => {
    'use strict';
    const root = document.getElementById('jevSettings');
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const url = '/api/global-master-templates/packages/settings';
    const field = id => root.querySelector('#' + id);
    const status = field('jevSettingsStatus');
    const save = field('packageSaveSettings');
    const test = field('packageTestSettings');
    let configured = false;
    function message(text, error = false) {
        status.textContent = text;
        status.dataset.error = String(error);
    }
    async function request(method = 'GET', data, suffix = '') {
        const response = await fetch(url + suffix, {method, credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: data ? JSON.stringify(data) : undefined});
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '决策模型配置请求失败，请检查输入后重试。');
        return body;
    }
    function showKeyStatus() {
        field('jevKeyStatus').textContent = configured ? '已保存 API Key；留空可保留，输入新密钥可替换。' : '尚未配置 API Key。';
    }
    function endpointHint() {
        field('jevEndpointHint').textContent = field('packageJevProtocol').value === 'openai'
            ? '填写完整请求地址，例如 /v1/chat/completions；不会自动追加路径。'
            : '填写完整请求地址，例如 /v1/systemone；不会自动追加路径。';
    }
    field('packageJevProtocol').addEventListener('change', () => {
        const endpoint = field('packageJevEndpoint');
        const defaults = ['https://api.typesafe.ai/v1/systemone', 'https://api.openai.com/v1/chat/completions'];
        if (defaults.includes(endpoint.value) || !endpoint.value.trim()) {
            endpoint.value = defaults[field('packageJevProtocol').value === 'openai' ? 1 : 0];
        }
        endpointHint();
    });
    save.disabled = test.disabled = true;
    request().then(settings => {
        configured = settings.configured;
        field('packageJevEnabled').checked = settings.enabled;
        field('packageJevModel').value = settings.model || 'jev-latest';
        field('packageJevEndpoint').value = settings.endpoint_url || 'https://api.typesafe.ai/v1/systemone';
        field('packageJevProtocol').value = settings.protocol || 'jev';
        endpointHint();
        field('packageJevTimeout').value = settings.timeout ?? 30;
        field('packageJevRetries').value = settings.retries ?? 2;
        field('packageJevConcurrency').value = settings.concurrency ?? 1;
        showKeyStatus();
    }).catch(error => {
        field('jevKeyStatus').textContent = '配置读取失败。';
        message(error.message, true);
    }).finally(() => { save.disabled = test.disabled = false; });
    async function submit(testOnly) {
        for (const input of root.querySelectorAll('input, select')) {
            if (!input.reportValidity()) return;
        }
        const key = field('packageJevKey').value.trim();
        const enabled = field('packageJevEnabled').checked;
        if ((enabled || testOnly) && !configured && !key) {
            message('请先填写 API Key。', true);
            field('packageJevKey').focus();
            return;
        }
        const payload = {enabled, model: field('packageJevModel').value.trim(),
            endpoint_url: field('packageJevEndpoint').value.trim(), protocol: field('packageJevProtocol').value,
            timeout: Number(field('packageJevTimeout').value), retries: Number(field('packageJevRetries').value),
            concurrency: Number(field('packageJevConcurrency').value)};
        if (key) payload.api_key = key;
        save.disabled = test.disabled = true;
        message(testOnly ? '正在测试决策模型…' : '正在保存…');
        try {
            if (testOnly) {
                const result = await request('POST', payload, '/test');
                message(`测试成功：${result.model}，耗时 ${result.elapsed_ms} 毫秒。当前表单尚未自动保存。`);
                return;
            }
            await request('PUT', payload);
            configured = configured || Boolean(key);
            field('packageJevKey').value = '';
            showKeyStatus();
            message('决策模型配置已保存。');
        } catch (error) { message(error.message, true); }
        finally { save.disabled = test.disabled = false; }
    }
    save.addEventListener('click', () => submit(false));
    test.addEventListener('click', () => submit(true));
})();
