/**
 * Reference renderer-side wake consumer for Antigravity App idle sessions.
 *
 * Key guarantees:
 * 1. Zero model / reasoning-effort / approval-policy changes (submits through target session's native composer).
 * 2. Phase 1 tries silent zero-switch submission inside mounted hidden DOM panes first.
 * 3. Phase 2 falls back to quick switch-and-return, deferring only when the user typed within the last 1.5s.
 * 4. Preserves any unsent draft in the target composer via 3-stage Lexical-safe clear + strict empty verification + restore.
 */

'use strict';

function installTaskBusWakeBridge(deps) {
    const {
        ipcRenderer,
        isVisible,
        findVisible,
        findVisibleComposer,
        findVisibleSendBtn,
        detectActiveConvId,
        findSidebarRowByCid,
        extractUuidFromReactFiber,
        showFlash = () => {}
    } = deps;

    if (typeof window === 'undefined' || window._agTaskBusWakeBridgeInstalled) return;
    window._agTaskBusWakeBridgeInstalled = true;

    const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
    const bridgeInstalledAt = Date.now();
    let wakeBusy = false;
    let lastUserTypingAt = 0;

    const markUserTyping = (ev) => {
        if (ev && ev.isTrusted) lastUserTypingAt = Date.now();
    };
    document.addEventListener('keydown', markUserTyping, true);
    document.addEventListener('compositionstart', markUserTyping, true);

    const sleepMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const getCurrentConvIdSafe = () => {
        const visibleNode = findVisible('[data-testid="user-input-step"], [data-testid="planner-response-text"]');
        return detectActiveConvId(null, visibleNode);
    };

    const getEditorText = (editor) => {
        if (!editor) return '';
        return String((editor.tagName === 'TEXTAREA' ? editor.value : editor.textContent) || '')
            .replace(/[\u200B-\u200D\uFEFF]/g, '')
            .trim();
    };

    const tryOpenConversation = async (targetCid) => {
        const targetLower = String(targetCid || '').toLowerCase();
        if (!targetLower) return false;
        const isTargetActive = () => {
            const cur = getCurrentConvIdSafe();
            return Boolean(cur && cur.toLowerCase() === targetLower);
        };
        if (isTargetActive()) return true;

        const clickRowIfPresent = () => {
            const row = findSidebarRowByCid(targetLower);
            if (!row) return false;
            try { row.scrollIntoView({ block: 'nearest' }); } catch (_e) {}
            (row.querySelector('a, button') || row).click();
            return true;
        };

        const waitForActivation = async (timeoutMs) => {
            const deadline = Date.now() + timeoutMs;
            while (Date.now() < deadline) {
                if (isTargetActive() && findVisibleComposer()) return true;
                await sleepMs(120);
            }
            return isTargetActive() && Boolean(findVisibleComposer());
        };

        if (clickRowIfPresent() && (await waitForActivation(2500))) return true;

        try {
            const expandBtns = Array.from(document.querySelectorAll('aside button, nav button, [data-testid*="sidebar"] button'));
            for (const btn of expandBtns) {
                if (!isVisible(btn)) continue;
                const t = (btn.textContent || '').replace(/\s+/g, ' ').trim();
                if (/^(Show\s*more|See\s*all|View\s*all|More|显示更多|查看更多|展开全部)$/i.test(t)) {
                    btn.click();
                    await sleepMs(350);
                    if (clickRowIfPresent() && (await waitForActivation(2500))) return true;
                    break;
                }
            }
        } catch (_e) {}

        try {
            const host = document.querySelector('main, [role="main"]') || document.body;
            if (host) {
                const link = document.createElement('a');
                link.href = 'conversation://' + targetLower;
                link.textContent = 'wake-nav';
                link.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0;';
                host.appendChild(link);
                try { link.click(); } finally {
                    setTimeout(() => { try { link.remove(); } catch (_re) {} }, 100);
                }
                if (await waitForActivation(2500)) return true;
            }
        } catch (_e) {}

        return isTargetActive() && Boolean(findVisibleComposer());
    };

    const fillAndSubmitComposer = async (editor, text) => {
        if (!editor || !text) return false;
        editor.focus();
        if (editor.tagName === 'TEXTAREA') {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(editor, text);
            else editor.value = text;
            editor.dispatchEvent(new Event('input', { bubbles: true }));
        } else {
            try {
                const sel = window.getSelection();
                if (sel) {
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }
            } catch (_se) {}
            try { document.execCommand('insertText', false, text); } catch (_e) {}
            await sleepMs(80);
            if (getEditorText(editor).length === 0) {
                try {
                    editor.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: text }));
                } catch (_be) {}
                await sleepMs(80);
                if (getEditorText(editor).length === 0) {
                    editor.textContent = text;
                    editor.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
                }
            }
        }

        await sleepMs(220);
        if (getEditorText(editor).length === 0) return false;

        const triggerSend = () => {
            const sendBtn = findVisibleSendBtn(editor);
            if (sendBtn && !sendBtn.disabled && sendBtn.getAttribute('aria-disabled') !== 'true') {
                sendBtn.click();
                return true;
            }
            try {
                editor.focus();
                const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
                editor.dispatchEvent(new KeyboardEvent('keydown', opts));
                editor.dispatchEvent(new KeyboardEvent('keypress', opts));
                editor.dispatchEvent(new KeyboardEvent('keyup', opts));
                return true;
            } catch (_e) {
                return false;
            }
        };

        triggerSend();
        const deadline = Date.now() + 2200;
        let retried = false;
        while (Date.now() < deadline) {
            await sleepMs(180);
            const curEditor = findVisibleComposer();
            if (!curEditor || getEditorText(curEditor).length === 0) return true;
            if (!retried && Date.now() > deadline - 1300) {
                retried = true;
                triggerSend();
            }
        }
        const finalEditor = findVisibleComposer();
        return !finalEditor || getEditorText(finalEditor).length === 0;
    };

    const findSendBtnInScope = (scope, editor) => {
        const root = scope || (editor && editor.closest('form')) || (editor && editor.parentElement && editor.parentElement.parentElement) || null;
        if (!root) return null;
        const btns = Array.from(root.querySelectorAll('button[type="submit"], button[aria-label*="Send" i], button[aria-label*="发送"], button[title*="Send" i], button[title*="发送"], button[data-testid*="send"]'))
            .filter((b) => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
        return btns.length > 0 ? btns[btns.length - 1] : null;
    };

    const findHiddenPaneComposerByCid = (targetCid) => {
        const targetLower = String(targetCid || '').toLowerCase();
        if (!targetLower) return null;
        const editors = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]')).filter((el) => {
            if (!el || !el.isConnected || isVisible(el)) return false;
            if (el.matches('[data-testid="ask-question-writein"]')) return false;
            if (el.closest('[role="dialog"], [data-testid="ask-question-writein"]')) return false;
            return true;
        });
        for (const ed of editors) {
            let matchedCid = null;
            const attrHost = ed.closest('[data-cascade-id], [data-conversation-id]');
            if (attrHost) {
                const v = attrHost.getAttribute('data-cascade-id') || attrHost.getAttribute('data-conversation-id') || '';
                const mv = v.match(UUID_RE);
                if (mv) matchedCid = mv[0];
            }
            if (!matchedCid) {
                let cur = ed;
                for (let d = 0; d < 12 && cur && cur !== document.body; d++) {
                    const fid = extractUuidFromReactFiber(cur);
                    if (fid) { matchedCid = fid; break; }
                    cur = cur.parentElement;
                }
            }
            if (matchedCid && matchedCid.toLowerCase() === targetLower) {
                const scope = attrHost || ed.closest('form') || (ed.parentElement && ed.parentElement.parentElement) || ed.parentElement;
                return { editor: ed, scope };
            }
        }
        return null;
    };

    const trySilentHiddenPaneWake = async (targetCid, wakeText) => {
        const found = findHiddenPaneComposerByCid(targetCid);
        if (!found || !found.editor) return { status: 'not_mounted' };
        const { editor, scope } = found;
        if (getEditorText(editor).length > 0) return { status: 'draft_present' };
        const prevActive = document.activeElement;
        try {
            if (editor.tagName === 'TEXTAREA') {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                if (setter) setter.call(editor, wakeText);
                else editor.value = wakeText;
                editor.dispatchEvent(new Event('input', { bubbles: true }));
                await sleepMs(100);
            } else {
                try {
                    editor.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: wakeText }));
                } catch (_be) {}
                await sleepMs(100);
                if (getEditorText(editor).length === 0) {
                    try {
                        const dt = new DataTransfer();
                        dt.setData('text/plain', wakeText);
                        editor.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt }));
                    } catch (_pe) {}
                    await sleepMs(100);
                }
            }
            if (getEditorText(editor).length === 0) return { status: 'cannot_fill_hidden' };

            for (let i = 0; i < 4; i++) {
                if (findSendBtnInScope(scope, editor)) break;
                await sleepMs(80);
            }
            const triggerHiddenSend = () => {
                const btn = findSendBtnInScope(scope, editor);
                if (btn) { btn.click(); return true; }
                try {
                    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
                    editor.dispatchEvent(new KeyboardEvent('keydown', opts));
                    editor.dispatchEvent(new KeyboardEvent('keypress', opts));
                    editor.dispatchEvent(new KeyboardEvent('keyup', opts));
                    return true;
                } catch (_ke) {
                    return false;
                }
            };
            triggerHiddenSend();
            const deadline = Date.now() + 1800;
            let retried = false;
            while (Date.now() < deadline) {
                await sleepMs(150);
                if (!editor.isConnected || getEditorText(editor).length === 0) return { status: 'submitted' };
                if (!retried && Date.now() > deadline - 1000) { retried = true; triggerHiddenSend(); }
            }
            return (!editor.isConnected || getEditorText(editor).length === 0)
                ? { status: 'submitted' }
                : { status: 'prefilled_unsubmitted' };
        } catch (_e) {
            return { status: 'error' };
        } finally {
            try {
                if (prevActive && prevActive.isConnected && document.activeElement !== prevActive && typeof prevActive.focus === 'function') {
                    prevActive.focus();
                }
            } catch (_fe) {}
        }
    };

    const clearComposerText = async (editor) => {
        if (!editor) return true;
        editor.focus();
        if (editor.tagName === 'TEXTAREA') {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(editor, '');
            else editor.value = '';
            editor.dispatchEvent(new Event('input', { bubbles: true }));
            await sleepMs(60);
            return getEditorText(editor).length === 0;
        }
        // Attempt 1: selectAll + delete
        try {
            editor.focus();
            document.execCommand('selectAll', false);
            document.dispatchEvent(new Event('selectionchange'));
        } catch (_e) {}
        await sleepMs(50);
        try { document.execCommand('delete', false); } catch (_e) {}
        await sleepMs(80);
        if (getEditorText(editor).length === 0) return true;

        // Attempt 2: Range + selectionchange + beforeinput(deleteContentBackward)
        try {
            editor.focus();
            const sel = window.getSelection();
            if (sel) {
                const range = document.createRange();
                range.selectNodeContents(editor);
                sel.removeAllRanges();
                sel.addRange(range);
            }
            document.dispatchEvent(new Event('selectionchange'));
        } catch (_e) {}
        await sleepMs(50);
        try {
            editor.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'deleteContentBackward' }));
        } catch (_e) {}
        await sleepMs(80);
        if (getEditorText(editor).length === 0) return true;

        // Attempt 3: Ctrl+A + Backspace
        try {
            editor.focus();
            const selAllOpts = { key: 'a', code: 'KeyA', keyCode: 65, which: 65, ctrlKey: true, bubbles: true, cancelable: true };
            editor.dispatchEvent(new KeyboardEvent('keydown', selAllOpts));
            editor.dispatchEvent(new KeyboardEvent('keyup', selAllOpts));
        } catch (_e) {}
        await sleepMs(50);
        try {
            const bsOpts = { key: 'Backspace', code: 'Backspace', keyCode: 8, which: 8, bubbles: true, cancelable: true };
            editor.dispatchEvent(new KeyboardEvent('keydown', bsOpts));
            editor.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'deleteContentBackward' }));
            editor.dispatchEvent(new KeyboardEvent('keyup', bsOpts));
        } catch (_e) {}
        await sleepMs(80);
        return getEditorText(editor).length === 0;
    };

    const restoreComposerText = async (editor, draftText) => {
        if (!editor || !draftText) return;
        editor.focus();
        if (editor.tagName === 'TEXTAREA') {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(editor, draftText);
            else editor.value = draftText;
            editor.dispatchEvent(new Event('input', { bubbles: true }));
        } else {
            try {
                const sel = window.getSelection();
                if (sel) {
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }
                document.execCommand('insertText', false, draftText);
            } catch (_e) {}
        }
        await sleepMs(80);
    };

    const pollWakeQueue = async () => {
        if (wakeBusy || (document.hidden === true && !document.body)) return;
        if (Date.now() - bridgeInstalledAt < 3500) return;
        if (document.querySelectorAll('[data-testid="conversation-row-sidebar"]').length === 0) return;
        wakeBusy = true;
        try {
            const claimed = await ipcRenderer.invoke('ag-task-bus:claim-wake');
            if (!claimed || !claimed.payload) return;
            const { fileName, payload } = claimed;
            const wakeId = String(payload.wakeId || '');
            const targetCid = String(payload.targetConversationId || '').trim();
            const wakeText = String(payload.wakeText || '').trim();
            const prevCid = getCurrentConvIdSafe();

            let prefilledBySilent = false;
            if (!prevCid || prevCid.toLowerCase() !== targetCid.toLowerCase()) {
                const silentRes = await trySilentHiddenPaneWake(targetCid, wakeText);
                if (silentRes.status === 'submitted') {
                    showFlash('已静默唤醒目标任务', 2000);
                    await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                        ok: true,
                        silentNoSwitch: true,
                        reason: 'submitted_via_hidden_pane',
                        targetConversationId: targetCid,
                        prevConversationId: prevCid || '',
                        returnedToPrev: true
                    });
                    return;
                }
                if (silentRes.status === 'prefilled_unsubmitted') prefilledBySilent = true;
            }

            if (Date.now() - lastUserTypingAt < 1500) {
                await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                    ok: false,
                    retryLater: true,
                    reason: 'user_actively_typing'
                });
                return;
            }

            const opened = await tryOpenConversation(targetCid);
            if (!opened) {
                if (prevCid && prevCid.toLowerCase() !== targetCid.toLowerCase()) await tryOpenConversation(prevCid);
                await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                    ok: false,
                    reason: 'target_conversation_not_found_or_not_activated',
                    targetConversationId: targetCid
                });
                return;
            }

            const verifiedCid = getCurrentConvIdSafe();
            if (!verifiedCid || verifiedCid.toLowerCase() !== targetCid.toLowerCase()) {
                if (prevCid && prevCid.toLowerCase() !== targetCid.toLowerCase()) await tryOpenConversation(prevCid);
                await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                    ok: false,
                    reason: 'target_verification_mismatch',
                    verifiedCid
                });
                return;
            }

            const targetEditor = findVisibleComposer();
            if (!targetEditor) {
                if (prevCid && prevCid.toLowerCase() !== targetCid.toLowerCase()) await tryOpenConversation(prevCid);
                await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                    ok: false,
                    reason: 'target_composer_not_found'
                });
                return;
            }

            const existingTargetText = getEditorText(targetEditor);
            let submitted = false;
            let savedTargetDraft = '';
            if (existingTargetText.length > 0) {
                if (prefilledBySilent && existingTargetText.indexOf(wakeText) !== -1) {
                    const sendBtn = findVisibleSendBtn(targetEditor);
                    if (sendBtn && !sendBtn.disabled && sendBtn.getAttribute('aria-disabled') !== 'true') sendBtn.click();
                    else {
                        targetEditor.focus();
                        const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
                        targetEditor.dispatchEvent(new KeyboardEvent('keydown', opts));
                        targetEditor.dispatchEvent(new KeyboardEvent('keypress', opts));
                        targetEditor.dispatchEvent(new KeyboardEvent('keyup', opts));
                    }
                    const dl = Date.now() + 2000;
                    while (Date.now() < dl) {
                        await sleepMs(150);
                        const ce = findVisibleComposer();
                        if (!ce || getEditorText(ce).length === 0) { submitted = true; break; }
                    }
                } else {
                    savedTargetDraft = existingTargetText;
                    const cleared = await clearComposerText(targetEditor);
                    if (!cleared || getEditorText(targetEditor).length > 0) {
                        if (prevCid && prevCid.toLowerCase() !== targetCid.toLowerCase()) await tryOpenConversation(prevCid);
                        await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                            ok: false,
                            retryLater: true,
                            reason: 'target_has_draft_clear_failed'
                        });
                        return;
                    }
                    submitted = await fillAndSubmitComposer(targetEditor, wakeText);
                    if (savedTargetDraft) {
                        const editorAfter = findVisibleComposer();
                        if (editorAfter && getEditorText(editorAfter).length === 0) {
                            await restoreComposerText(editorAfter, savedTargetDraft);
                        }
                    }
                }
            } else {
                submitted = await fillAndSubmitComposer(targetEditor, wakeText);
            }
            await sleepMs(150);

            let returnedToPrev = false;
            if (prevCid && prevCid.toLowerCase() !== targetCid.toLowerCase()) {
                returnedToPrev = await tryOpenConversation(prevCid);
            }
            if (submitted) showFlash('已自动唤醒目标任务', 2000);
            await ipcRenderer.invoke('ag-task-bus:finish-wake', wakeId, fileName, {
                ok: Boolean(submitted),
                silentNoSwitch: false,
                draftPreserved: Boolean(savedTargetDraft),
                reason: submitted ? 'submitted_via_app_ui_fallback' : 'composer_submit_unconfirmed',
                targetConversationId: targetCid,
                prevConversationId: prevCid || '',
                returnedToPrev
            });
        } catch (_e) {
        } finally {
            wakeBusy = false;
        }
    };

    setInterval(() => { pollWakeQueue().catch(() => {}); }, 1200);
}

module.exports = { installTaskBusWakeBridge };
