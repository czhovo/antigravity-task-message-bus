/**
 * Reference Electron main-process IPC handlers for Antigravity App idle session wake bridge.
 *
 * Registers:
 *   - ag-task-bus:claim-wake
 *   - ag-task-bus:finish-wake
 *
 * Consumes ~/.gemini/antigravity/task_bus/app_wake_queue/*.json
 * Writes   ~/.gemini/antigravity/task_bus/app_wake_results/<wakeId>.json
 */

'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

function registerTaskBusWakeIpc(ipcMain, options = {}) {
    if (!ipcMain || typeof ipcMain.handle !== 'function') return;
    if (globalThis.__agTaskBusWakeIpcRegistered) return;
    globalThis.__agTaskBusWakeIpcRegistered = true;

    const busBase = options.busBase || path.join(os.homedir(), '.gemini', 'antigravity', 'task_bus');
    const wakeQueueDir = path.join(busBase, 'app_wake_queue');
    const wakeResultsDir = path.join(busBase, 'app_wake_results');
    const inFlight = new Map();
    const deferred = new Map();

    const writeWakeResult = (wakeId, resultObj) => {
        try {
            fs.mkdirSync(wakeResultsDir, { recursive: true });
            const tmpP = path.join(wakeResultsDir, '.' + wakeId + '.tmp');
            const finalP = path.join(wakeResultsDir, wakeId + '.json');
            const payload = Object.assign({ wakeId, finishedAt: new Date().toISOString() }, resultObj || {});
            fs.writeFileSync(tmpP, JSON.stringify(payload, null, 2), 'utf8');
            fs.renameSync(tmpP, finalP);
            const files = fs.readdirSync(wakeResultsDir).filter((f) => f.endsWith('.json')).sort();
            if (files.length > 50) {
                for (const oldF of files.slice(0, files.length - 50)) {
                    try { fs.unlinkSync(path.join(wakeResultsDir, oldF)); } catch (_e) {}
                }
            }
        } catch (_e) {}
    };

    ipcMain.handle('ag-task-bus:claim-wake', () => {
        try {
            if (!fs.existsSync(wakeQueueDir)) return null;
            const now = Date.now();
            for (const [k, ts] of inFlight.entries()) {
                if (now - ts > 25000) inFlight.delete(k);
            }
            const files = fs.readdirSync(wakeQueueDir)
                .filter((f) => f.endsWith('.json') && !f.startsWith('.'))
                .sort();
            for (const fileName of files) {
                const fullPath = path.join(wakeQueueDir, fileName);
                let payload = null;
                try {
                    payload = JSON.parse(fs.readFileSync(fullPath, 'utf8'));
                } catch (_pe) {
                    try { fs.unlinkSync(fullPath); } catch (_ue) {}
                    continue;
                }
                if (!payload || !payload.wakeId || !payload.targetConversationId) {
                    try { fs.unlinkSync(fullPath); } catch (_ue) {}
                    continue;
                }
                const wakeId = String(payload.wakeId);
                if (inFlight.has(wakeId)) continue;
                const defInfo = deferred.get(wakeId);
                if (defInfo && now < defInfo.until) continue;
                const ageMs = now - Number(payload.createdAtMs || 0);
                if (payload.createdAtMs && ageMs > 600000) {
                    writeWakeResult(wakeId, { ok: false, reason: 'expired' });
                    try { fs.unlinkSync(fullPath); } catch (_ue) {}
                    deferred.delete(wakeId);
                    continue;
                }
                inFlight.set(wakeId, now);
                return { fileName, payload };
            }
            return null;
        } catch (_e) {
            return null;
        }
    });

    ipcMain.handle('ag-task-bus:finish-wake', (_event, wakeId, fileName, resultObj) => {
        try {
            const idStr = String(wakeId || '');
            inFlight.delete(idStr);
            const fullPath = fileName ? path.join(wakeQueueDir, String(fileName)) : '';
            if (resultObj && resultObj.retryLater) {
                const prev = deferred.get(idStr) || { count: 0, until: 0 };
                const nextCount = prev.count + 1;
                const reasonStr = String(resultObj.reason || '');
                const isDraftWait = reasonStr.indexOf('has_draft') !== -1;
                const maxRetries = isDraftWait ? 120 : 6;
                if (nextCount <= maxRetries) {
                    deferred.set(idStr, { count: nextCount, until: Date.now() + 5000 });
                    return { ok: true, deferred: true, attempt: nextCount, maxRetries };
                }
                resultObj = Object.assign({}, resultObj, {
                    ok: false,
                    retryLater: false,
                    reason: (reasonStr || 'deferred') + '_max_retries'
                });
            }
            deferred.delete(idStr);
            if (idStr) {
                writeWakeResult(idStr, resultObj || { ok: false, reason: 'unknown' });
            }
            if (fullPath && fs.existsSync(fullPath)) {
                try { fs.unlinkSync(fullPath); } catch (_ue) {}
            }
            return { ok: true };
        } catch (_e) {
            return { ok: false };
        }
    });
}

module.exports = { registerTaskBusWakeIpc };
