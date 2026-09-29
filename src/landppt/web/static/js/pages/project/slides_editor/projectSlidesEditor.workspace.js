/* Layout controls only; slide data and editing actions remain with the editor. */
(() => {
    const container = document.querySelector('.editor-container');
    const sidebar = document.getElementById('slidesSidebar');
    const toggle = document.getElementById('slidesSidebarToggle');
    const backdrop = document.querySelector('.workspace-sidebar-backdrop');
    const more = document.querySelector('.workspace-more');
    const mobile = window.matchMedia('(max-width: 700px)');
    if (!container || !sidebar || !toggle) return;
    const toolbar = document.querySelector('.editor-toolbar');
    toolbar.addEventListener('show.bs.dropdown', () => {
        toolbar.style.setProperty('--workspace-menu-top', `${toolbar.getBoundingClientRect().bottom + 6}px`);
    });

    function setSidebar(open) {
        container.classList.toggle('sidebar-collapsed', !open);
        toggle.setAttribute('aria-expanded', String(open));
        backdrop.hidden = !open || !mobile.matches;
        if (typeof debounceResize === 'function') debounceResize();
    }
    setSidebar(!mobile.matches);
    toggle.addEventListener('click', () => setSidebar(container.classList.contains('sidebar-collapsed')));
    backdrop.addEventListener('click', () => setSidebar(false));
    mobile.addEventListener('change', () => setSidebar(!mobile.matches));
    sidebar.addEventListener('click', event => {
        if (mobile.matches && event.target.closest('.slide-thumbnail') && !event.ctrlKey && !event.metaKey) {
            setSidebar(false);
        }
    }, true);
    document.addEventListener('click', event => {
        if (!more.contains(event.target) || event.target.closest('.workspace-more-menu button')) more.open = false;
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        if (more.open) { more.open = false; more.querySelector('summary').focus(); }
        if (mobile.matches && !container.classList.contains('sidebar-collapsed')) {
            setSidebar(false);
            toggle.focus();
        }
    });
})();
