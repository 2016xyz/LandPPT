/* Navigation and focus management for existing application controls. */
(function () {
    'use strict';
    const header = document.querySelector('.app-header, .admin-body > .header');
    let toggle = document.getElementById('navToggle');
    let nav = document.getElementById('appNavigation');
    if (header && !nav && document.body.classList.contains('admin-body')) {
        header.classList.add('admin-header');
        nav = header.querySelector('.header-links');
        if (nav) {
            nav.id = 'adminNavigation';
            nav.setAttribute('role', 'navigation');
            nav.setAttribute('aria-label', '后台导航');
            toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.id = 'adminNavToggle';
            toggle.className = 'nav-toggle';
            toggle.textContent = '菜单';
            toggle.setAttribute('aria-controls', nav.id);
            toggle.setAttribute('aria-expanded', 'false');
            header.insertBefore(toggle, nav);
        }
    }
    if (header && toggle && nav) {
        const setOpen = open => {
            header.classList.toggle('is-nav-open', open);
            toggle.setAttribute('aria-expanded', String(open));
        };
        toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
        header.addEventListener('keydown', event => {
            if (event.key === 'Escape' && header.classList.contains('is-nav-open')) {
                setOpen(false);
                toggle.focus();
            }
        });
        document.addEventListener('click', event => {
            if (!header.contains(event.target)) setOpen(false);
        });
        matchMedia('(max-width: 1000px)').addEventListener('change', () => setOpen(false));
        const path = location.pathname.replace(/\/$/, '') || '/home';
        const section = path === '/scenarios' ? '/create' : path.startsWith('/projects/') ? '/projects' : path.startsWith('/admin') ? '/admin' : path;
        nav.querySelectorAll('a[href^="/"]').forEach(link => {
            if (new URL(link.href).pathname === section) link.setAttribute('aria-current', 'page');
        });
    }

    const modals = new Map();
    let previousOverflow = '';
    const focusable = modal => [...modal.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')]
        .filter(el => el.getClientRects().length && !el.closest('[hidden], [inert]'));
    const activeModal = () => [...modals.keys()].at(-1);
    function openModal(modal, initialFocus, options = {}) {
        if (!modal) return;
        if (!modals.has(modal)) {
            const lockScroll = options.lockScroll !== false;
            if (lockScroll && ![...modals.values()].some(entry => entry.lockScroll)) {
                previousOverflow = document.body.style.overflow;
                document.body.style.overflow = 'hidden';
            }
            modals.set(modal, {focus: document.activeElement, lockScroll});
        }
        if (getComputedStyle(modal).display === 'none') modal.style.display = 'flex';
        modal.tabIndex = -1;
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        const title = modal.querySelector('h2, h3, h4, h5, #loadingOverlayMessage');
        if (title) {
            if (!title.id) title.id = modal.id + 'Title';
            modal.setAttribute('aria-labelledby', title.id);
        }
        (initialFocus || focusable(modal)[0] || modal).focus({preventScroll: true});
    }
    function closeModal(modal, options = {}) {
        if (!modal) return;
        if (!options.preserveDisplay) modal.style.display = 'none';
        if (!modals.has(modal)) return;
        const previous = modals.get(modal);
        modals.delete(modal);
        if (previous.lockScroll && ![...modals.values()].some(entry => entry.lockScroll)) document.body.style.overflow = previousOverflow;
        const active = activeModal();
        if (active) (focusable(active)[0] || active).focus({preventScroll: true});
        else if (previous.focus?.isConnected) previous.focus.focus({preventScroll: true});
    }
    document.addEventListener('keydown', event => {
        const modal = activeModal();
        if (!modal) return;
        if (!modal.contains(event.target) && event.target.closest('dialog[open], .ln-dialog-overlay')) return;
        if (event.key === 'Escape' && modal.classList.contains('modal')) {
            const close = modal.querySelector('button.close, button.close-btn, button.modal-close');
            if (close && !close.disabled && modal.getAttribute('aria-busy') !== 'true') {
                event.preventDefault();
                event.stopPropagation();
                close.click();
            }
            return;
        }
        if (event.key !== 'Tab') return;
        const items = focusable(modal);
        const current = items.indexOf(document.activeElement);
        if (!items.length) {
            event.preventDefault();
            modal.focus();
        } else if (current < 0 || (event.shiftKey && current === 0) || (!event.shiftKey && current === items.length - 1)) {
            event.preventDefault();
            items[event.shiftKey ? items.length - 1 : 0].focus();
        }
    }, true);
    document.addEventListener('focusin', event => {
        const modal = activeModal();
        // Native/shared dialogs may open above the current application modal.
        if (modal && !modal.contains(event.target) && !event.target.closest('dialog[open], .ln-dialog-overlay')) {
            (focusable(modal)[0] || modal).focus({preventScroll: true});
        }
    });
    window.LandPPTUI = {openModal, closeModal};
    document.addEventListener('click', event => {
        document.querySelectorAll('.ui-action-menu[open]').forEach(menu => {
            const action = event.target.closest('button, a[href]');
            if (!menu.contains(event.target) || (action && menu.contains(action))) {
                menu.open = false;
                if (action && menu.contains(action)) menu.querySelector('summary').focus({preventScroll: true});
            }
        });
    }, true);
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || activeModal() || event.target.closest('dialog[open], .ln-dialog-overlay')) return;
        const menu = event.target.closest('.ui-action-menu[open]');
        if (!menu) return;
        menu.open = false;
        menu.querySelector('summary').focus({preventScroll: true});
        event.preventDefault();
    });
    // Observe the existing modal lifecycle; keep each page's close/save handlers
    // and scroll management, and leave Bootstrap/native presentation dialogs alone.
    document.querySelectorAll('.landppt-app .modal, .admin-body .modal').forEach(modal => {
        modal.querySelectorAll('button.close, button.close-btn, button.modal-close').forEach(button => {
            if (!button.hasAttribute('aria-label')) button.setAttribute('aria-label', '关闭弹窗');
        });
        const sync = () => {
            const visible = getComputedStyle(modal).display !== 'none' && modal.getClientRects().length > 0;
            if (visible && !modals.has(modal)) openModal(modal, null, {lockScroll: false});
            else if (!visible && modals.has(modal)) closeModal(modal, {preserveDisplay: true});
        };
        new MutationObserver(sync).observe(modal, {attributes: true, attributeFilter: ['style', 'class', 'hidden']});
        sync();
    });
})();
