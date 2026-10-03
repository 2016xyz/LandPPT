/**
 * LandPPT 无人值守任务监控面板
 *
 * 挂载为 window.UnattendedMonitor，零依赖，可用于 base.html 页面与独立文档页面：
 *   UnattendedMonitor.mount({ container: 'unattendedMonitor', projectId: 'xxx' })
 *
 * 面板在项目没有无人值守任务时保持隐藏；任务运行中轮询状态，任务终态后停止轮询。
 */
(function () {
    'use strict';

    if (window.UnattendedMonitor) return;

    var ACTIVE_INTERVAL_MS = 5000;
    var MAX_INTERVAL_MS = 60000;
    var TERMINAL = { completed: 1, failed: 1, cancelled: 1 };

    var STATUS_TEXT = {
        running: '运行中',
        pending: '等待中',
        queued: '排队中',
        completed: '已完成',
        failed: '已失败',
        cancelled: '已取消'
    };

    var STAGE_ICONS = {
        completed: 'fa-circle-check',
        running: 'fa-spinner fa-spin',
        failed: 'fa-circle-xmark',
        cancelled: 'fa-ban',
        skipped: 'fa-minus',
        pending: 'fa-circle'
    };

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function resolveElement(target) {
        if (!target) return null;
        return typeof target === 'string' ? document.getElementById(target) : target;
    }

    function notify(type, message) {
        if (window.Notify && typeof window.Notify[type] === 'function') {
            window.Notify[type](message);
        }
    }

    function Monitor(options) {
        this.container = resolveElement(options.container);
        this.projectId = options.projectId;
        this.compact = Boolean(options.compact);
        this.onComplete = typeof options.onComplete === 'function' ? options.onComplete : null;
        // Fired on every poll so the host page can react to stage transitions
        // (e.g. render the outline the moment the outline stage finishes).
        this.onUpdate = typeof options.onUpdate === 'function' ? options.onUpdate : null;
        // True when the server rendered this page while a run was still live. Only then
        // is a terminal status news: revisiting a project whose run finished days ago
        // must not toast or reload.
        this.wasActive = Boolean(options.initiallyActive);
        this.reportedTerminal = false;
        this.interval = ACTIVE_INTERVAL_MS;
        this.timer = null;
        this.stopped = false;
        this.destroyed = false;
    }

    Monitor.prototype.start = function () {
        if (!this.container || !this.projectId) return;
        var self = this;
        this.container.classList.add('lu-monitor');
        this.container.hidden = true;

        // 页面隐藏时不轮询，返回时立刻刷新一次。
        this._onVisibility = function () {
            if (!document.hidden && !self.stopped) self.refresh();
        };
        document.addEventListener('visibilitychange', this._onVisibility);

        this._onUnload = function () { self.destroy(); };
        window.addEventListener('pagehide', this._onUnload);
        window.addEventListener('beforeunload', this._onUnload);

        this.refresh();
    };

    Monitor.prototype.schedule = function () {
        if (this.stopped || this.destroyed) return;
        var self = this;
        clearTimeout(this.timer);
        this.timer = setTimeout(function () { self.refresh(); }, this.interval);
    };

    Monitor.prototype.refresh = function () {
        if (this.stopped || this.destroyed) return;
        if (document.hidden) { this.schedule(); return; }

        var self = this;
        fetch('/api/projects/' + encodeURIComponent(this.projectId) + '/unattended/status', {
            headers: { 'Accept': 'application/json' }
        })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            })
            .then(function (data) {
                self.interval = ACTIVE_INTERVAL_MS;
                self.render(data && data.run ? data.run : null);
                if (!data || !data.run) {
                    // 没有任务：不再轮询，避免对普通项目产生无谓请求。
                    self.stopped = true;
                    return;
                }
                if (TERMINAL[data.run.task_status]) {
                    self.stopped = true;
                    return;
                }
                self.schedule();
            })
            .catch(function () {
                // 出错时指数退避，避免服务异常时刷屏。
                self.interval = Math.min(MAX_INTERVAL_MS, self.interval * 2);
                self.schedule();
            });
    };

    Monitor.prototype.render = function (run) {
        if (!this.container) return;
        if (!run) {
            this.container.hidden = true;
            this.container.innerHTML = '';
            return;
        }

        var status = run.status || run.task_status || 'running';
        var badgeClass = TERMINAL[run.task_status] ? run.task_status : 'running';
        var progress = Math.max(0, Math.min(100, Number(run.overall_progress) || 0));
        // Preserve the user's disclosure choice across polling renders.
        var previousDetails = this.container.querySelector('.lu-monitor-details');
        var detailsOpen = previousDetails ? previousDetails.open : run.task_status === 'failed';
        var detailsFocused = previousDetails && previousDetails.querySelector('summary') === document.activeElement;

        var allStages = run.stages || [];
        // Stages the user did not select are noise: with the default stop_after
        // half the list would be dimmed rows about work that will never happen.
        var plannedStages = allStages.filter(function (s) { return s.status !== 'skipped'; });
        var skippedCount = allStages.length - plannedStages.length;

        var stagesHtml = plannedStages.map(function (stage) {
            var stageStatus = stage.status || 'pending';
            var icon = STAGE_ICONS[stageStatus] || STAGE_ICONS.pending;
            var detail = stage.error || stage.message || '';
            return '' +
                '<li class="lu-monitor-stage is-' + escapeHtml(stageStatus) + '">' +
                '<span class="lu-monitor-stage-icon"><i class="fas ' + icon + '"></i></span>' +
                '<span class="lu-monitor-stage-body">' +
                '<span class="lu-monitor-stage-name">' + escapeHtml(stage.name || stage.id) + '</span>' +
                (detail
                    ? '<div class="lu-monitor-stage-msg' + (stage.error ? ' is-error' : '') + '">' + escapeHtml(detail) + '</div>'
                    : '') +
                '</span>' +
                '</li>';
        }).join('');

        var actionsHtml = '';
        if (!TERMINAL[run.task_status]) {
            actionsHtml += '<button type="button" class="lu-monitor-btn" data-lu-action="cancel">' +
                '<i class="fas fa-stop"></i> 停止</button>';
        }
        if (run.download_url) {
            actionsHtml += '<a class="lu-monitor-btn lu-monitor-btn--primary" href="' +
                escapeHtml(run.download_url) + '"><i class="fas fa-download"></i> 下载视频</a>';
        }

        var currentStage = plannedStages.filter(function (s) { return s.status === 'running'; })[0];
        var doneCount = plannedStages.filter(function (s) { return s.status === 'completed'; }).length;
        var subtitle = TERMINAL[run.task_status]
            ? (run.topic || '')
            : (currentStage
                ? '正在' + (currentStage.name || '') + ' · 第 ' + (doneCount + 1) + '/' + plannedStages.length + ' 步'
                : (run.topic || ''));
        var stageDetails = '<ul class="lu-monitor-stages">' + stagesHtml + '</ul>' +
            (skippedCount
                ? '<div class="lu-monitor-skipped">另有 ' + skippedCount + ' 个阶段未选择，不会执行</div>'
                : '');
        if (this.compact) {
            stageDetails = '<details class="lu-monitor-details"' + (detailsOpen ? ' open' : '') + '>' +
                '<summary>阶段详情 · ' + doneCount + '/' + plannedStages.length + ' 已完成</summary>' +
                stageDetails + '</details>';
        }

        this.container.hidden = false;
        this.container.innerHTML = '' +
            '<div class="lu-monitor-head">' +
            '<div class="lu-monitor-heading">' +
            '<h3 class="lu-monitor-title"><i class="fas fa-robot"></i> 无人值守任务</h3>' +
            (subtitle ? '<div class="lu-monitor-topic" title="' + escapeHtml(subtitle) + '">' + escapeHtml(subtitle) + '</div>' : '') +
            '</div>' +
            '<span class="lu-monitor-badge lu-monitor-badge--' + escapeHtml(badgeClass) + '">' +
            escapeHtml(STATUS_TEXT[status] || status) + '</span>' +
            (actionsHtml ? '<div class="lu-monitor-actions">' + actionsHtml + '</div>' : '') +
            '</div>' +
            '<div class="lu-monitor-bar"><div class="lu-monitor-bar-fill' +
            (TERMINAL[run.task_status] ? ' is-' + escapeHtml(run.task_status) : '') +
            '" style="width:' + progress + '%"></div></div>' +
            '<div class="lu-monitor-meta">总进度 ' + progress.toFixed(0) + '%</div>' +
            stageDetails +
            // A cancel carries an `error` string too; showing it in a red error box
            // would read as a failure the user did not cause.
            (run.error && run.task_status !== 'cancelled'
                ? '<div class="lu-monitor-error">' + escapeHtml(run.error) + '</div>'
                : '');

        if (this.compact && detailsFocused) {
            this.container.querySelector('.lu-monitor-details summary').focus({ preventScroll: true });
        }

        var cancelBtn = this.container.querySelector('[data-lu-action="cancel"]');
        if (cancelBtn) {
            var self = this;
            cancelBtn.addEventListener('click', function () { self.cancel(cancelBtn); });
        }

        if (this.onUpdate) {
            try { this.onUpdate(run); } catch (e) { /* 回调异常不影响面板 */ }
        }

        if (!TERMINAL[run.task_status]) {
            // Seen alive by this monitor, so its eventual terminal status is news even
            // if the page was rendered before the server knew about the run.
            this.wasActive = true;
        } else if (this.wasActive && !this.reportedTerminal) {
            // Report on the FIRST terminal observation. A short run can finish between
            // the server render and the first poll, and that must still be announced.
            this.reportedTerminal = true;
            if (run.task_status === 'completed') {
                notify('success', '无人值守任务已完成');
            } else if (run.task_status === 'failed') {
                notify('error', '无人值守任务失败：' + (run.error || '未知原因'));
            } else {
                notify('warning', '无人值守任务已取消');
            }
            if (this.onComplete) {
                try { this.onComplete(run); } catch (e) { /* 回调异常不影响面板 */ }
            }
        }
    };

    Monitor.prototype.cancel = function (button) {
        var self = this;
        var proceed = window.Notify
            ? window.Notify.confirm('确定要停止无人值守任务吗？当前阶段结束后停止。', { danger: true })
            : Promise.resolve(true);

        proceed.then(function (confirmed) {
            if (!confirmed) return;
            if (button) button.disabled = true;
            return fetch('/api/projects/' + encodeURIComponent(self.projectId) + '/unattended/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                })
                .then(function () {
                    notify('info', '已请求停止，当前阶段结束后生效');
                    self.stopped = false;
                    self.refresh();
                })
                .catch(function (error) {
                    if (button) button.disabled = false;
                    notify('error', '停止失败：' + error.message);
                });
        });
    };

    Monitor.prototype.destroy = function () {
        this.destroyed = true;
        this.stopped = true;
        clearTimeout(this.timer);
        if (this._onVisibility) document.removeEventListener('visibilitychange', this._onVisibility);
        if (this._onUnload) {
            window.removeEventListener('pagehide', this._onUnload);
            window.removeEventListener('beforeunload', this._onUnload);
        }
    };

    window.UnattendedMonitor = {
        mount: function (options) {
            var monitor = new Monitor(options || {});
            monitor.start();
            return monitor;
        }
    };
})();
