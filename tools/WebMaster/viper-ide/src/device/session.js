/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Session core: the device session state machine, extracted from app.js
 * (v0.6.5) as chunk 1 of the B2b extraction. Pure module: no DOM, no
 * transports. The state lives here; transport and UI wiring arrive in later
 * chunks. Semantics mirror app.js:
 *
 *   state: disconnected | busy-initial | busy-running | ready | reconnecting
 *
 * Run mode is orthogonal to session state: running a script from the editor
 * keeps the session 'ready' (raw mode is held, the board keeps answering).
 */

export const DEVICE_STATES = Object.freeze([
    'disconnected', 'busy-initial', 'busy-running', 'ready', 'reconnecting',
])

export function isBusyState(state) {
    return state === 'busy-initial' || state === 'busy-running'
}

export function createSessionCore({ onChange = () => {} } = {}) {
    let state = 'disconnected'
    let runMode = false
    let sessionInitialized = false
    let probeInFlight = false
    let reconnectToken = 0
    let intentionalDisconnect = false
    let draftsRestored = false

    return {
        get state() { return state },
        get isBusy() { return isBusyState(state) },
        get isReady() { return state === 'ready' },
        get runMode() { return runMode },
        get sessionInitialized() { return sessionInitialized },
        get probeInFlight() { return probeInFlight },
        get intentionalDisconnect() { return intentionalDisconnect },
        get draftsRestored() { return draftsRestored },

        setState(next) {
            if (!DEVICE_STATES.includes(next)) {
                throw new Error(`session: unknown state '${String(next)}'`)
            }
            if (next === state) { return }
            const prev = state
            state = next
            onChange('state', { prev, next })
        },

        setRunMode(on) {
            on = !!on
            if (on === runMode) { return }
            runMode = on
            onChange('runMode', { on })
        },

        setSessionInitialized(v) { sessionInitialized = !!v },
        setDraftsRestored(v) { draftsRestored = !!v },
        markIntentional(v) { intentionalDisconnect = !!v },

        /* Reconnect loop: stop() makes every in-flight token stale. */
        beginReconnect() { reconnectToken++; return reconnectToken },
        isReconnectCurrent(token) { return token === reconnectToken },
        stopReconnect() { reconnectToken++ },

        /* Single-flight REPL probes (soft-reset / prompt-settled). */
        probeBegin() {
            if (probeInFlight) { return false }
            probeInFlight = true
            return true
        },
        probeEnd() { probeInFlight = false },
    }
}

export default createSessionCore
