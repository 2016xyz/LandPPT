(() => {
    'use strict';
    const tabs = [...document.querySelectorAll('[data-template-tab]')];
    if (!tabs.length) return;
    function select(value, updateUrl = true) {
        for (const tab of tabs) {
            const active = tab.dataset.templateTab === value;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
        }
        if (updateUrl) {
            const url = new URL(location.href);
            url.searchParams.set('tab', value);
            history.replaceState(null, '', url);
        }
    }
    function restore() {
        select(new URLSearchParams(location.search).get('tab') === 'packages' ? 'packages' : 'regular', false);
    }
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => select(tab.dataset.templateTab));
        tab.addEventListener('keydown', event => {
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = tabs.length - 1;
            else return;
            event.preventDefault();
            select(tabs[next].dataset.templateTab);
            tabs[next].focus();
        });
    });
    window.addEventListener('popstate', restore);
    restore();
})();
