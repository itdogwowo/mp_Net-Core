/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Minimal publish/subscribe bus. Zero dependencies, no DOM.
 * Own modules talk to each other through buses and the registry, never by
 * importing each other's internals.
 */

export function createBus() {
    const listeners = new Map()   // name -> Set<fn>

    function on(name, fn) {
        if (!listeners.has(name)) { listeners.set(name, new Set()) }
        listeners.get(name).add(fn)
        return () => off(name, fn)
    }

    function off(name, fn) {
        const set = listeners.get(name)
        if (set) { set.delete(fn) }
    }

    function once(name, fn) {
        const wrap = (...args) => { off(name, wrap); fn(...args) }
        return on(name, wrap)
    }

    function emit(name, ...args) {
        const set = listeners.get(name)
        if (!set) { return }
        for (const fn of [...set]) { fn(...args) }
    }

    return { on, once, off, emit, clear: () => listeners.clear() }
}

export default createBus
