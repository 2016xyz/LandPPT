/* UI semantics only: field values and existing save handlers stay with each page. */
(() => {
    'use strict';
    const root = document.querySelector('.settings-page');
    if (!root) return;
    let serial = 0;
    const idFor = (element, prefix) => element.id || (element.id = prefix + (++serial));

    root.querySelectorAll('.form-group').forEach(group => {
        const label = group.querySelector('label');
        const field = group.querySelector('input:not([type=hidden]), select, textarea');
        if (!field) return;
        if (label && !label.htmlFor && !label.contains(field)) label.htmlFor = idFor(field, 'settings-field-');
        const help = group.querySelector('small, .field-help, .field-hint, .form-hint');
        if (help) {
            const describedBy = new Set((field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
            describedBy.add(idFor(help, 'settings-hint-'));
            field.setAttribute('aria-describedby', [...describedBy].join(' '));
        }
    });
    root.querySelectorAll('.param-card').forEach(card => {
        const label = card.querySelector('.param-card-label');
        if (!label) return;
        const labelId = idFor(label, 'settings-param-');
        card.querySelectorAll('input').forEach(field => field.setAttribute('aria-labelledby', labelId));
    });
    root.querySelectorAll('.password-toggle button').forEach(button => {
        const field = button.parentElement.querySelector('input');
        const label = button.closest('.form-group')?.querySelector('label')?.textContent.trim() || '密码';
        if (!button.hasAttribute('aria-label')) button.setAttribute('aria-label', '显示或隐藏' + label);
        const sync = () => button.setAttribute('aria-pressed', String(field?.type !== 'password'));
        sync();
        button.addEventListener('click', () => queueMicrotask(sync));
    });
    root.querySelectorAll('.section-header').forEach(header => {
        if (header.querySelector('button[id^=save]')) header.classList.add('settings-section-header-sticky');
    });

    // Let the original click/hash handlers activate panels and load their data.
    root.querySelectorAll('.community-tabs, .tabs').forEach(list => {
        const tabs = [...list.querySelectorAll('.community-tab, .tab')];
        if (!tabs.length) return;
        list.setAttribute('role', 'tablist');
        if (!list.hasAttribute('aria-label')) list.setAttribute('aria-label', '设置分类');
        const sync = () => tabs.forEach(tab => {
            const selected = tab.matches('.is-active, .active');
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            const panelId = tab.getAttribute('aria-controls');
            const panel = panelId && document.getElementById(panelId);
            if (panel) {
                panel.setAttribute('role', 'tabpanel');
                panel.setAttribute('aria-labelledby', idFor(tab, 'settings-tab-'));
            }
        });
        sync();
        list.addEventListener('click', () => queueMicrotask(sync));
        window.addEventListener('hashchange', () => queueMicrotask(sync));
        list.addEventListener('keydown', event => {
            const index = tabs.indexOf(event.target.closest('[role=tab]'));
            if (index < 0) return;
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = tabs.length - 1;
            else return;
            event.preventDefault();
            tabs[next].click();
            tabs[next].focus();
            tabs[next].scrollIntoView({block: 'nearest', inline: 'nearest'});
        });
    });
    const header = document.querySelector('.settings-admin > .header');
    if (header) {
        const measure = () => root.style.setProperty('--settings-header-height', header.getBoundingClientRect().height + 'px');
        new ResizeObserver(measure).observe(header);
        measure();
    }
    root.querySelectorAll('#image-service-status, #systemLandPptStatus, #githubStatus, #linuxdoStatus, #testResult, #testResults').forEach(status => {
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
    });
})();
