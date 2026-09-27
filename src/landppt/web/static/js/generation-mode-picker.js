(() => {
    'use strict';
    const buttons = [...document.querySelectorAll('[data-generation-mode]')];
    if (!buttons.length) return;
    const packages = document.getElementById('templatePackagePanel');
    const regular = document.getElementById('regularTemplatePanel');
    const hint = document.getElementById('generationModeHint');
    function select(mode) {
        const fast = mode === 'package';
        packages.hidden = !fast;
        regular.hidden = fast;
        buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.generationMode === mode)));
        hint.textContent = fast ? '选择一个已发布的模板包以启用快速模式；首次使用可先添加内置模板包。' : '选择下方模板后开始生成。';
        const url = new URL(window.location.href);
        url.searchParams.set('mode', mode);
        history.replaceState(null, '', url);
    }
    buttons.forEach(button => button.addEventListener('click', () => select(button.dataset.generationMode)));
    const requested = new URLSearchParams(location.search).get('mode');
    // A package picked at requirement confirmation shaped the outline; open on it.
    const preferred = packages && packages.dataset.preferredVersion ? 'package' : 'freeform';
    select(requested === 'package' || requested === 'freeform' ? requested : preferred);
})();
