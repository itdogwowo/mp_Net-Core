/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Connection catalog: what connection kinds exist, how connection strings are
 * normalized/classified, and when a kind is usable in the current browser.
 * Pure module (no DOM): every decision here is unit-testable. The UI layer
 * (session.js / app) turns the results into prompts and toasts.
 *
 * Semantics preserved from src/app.js prepareNewPort() / URL-parameter
 * handling (v0.6.5) so behaviour stays identical during modularization.
 */

import { ConnectionUID } from '../connection_uid.js'

/* Toolbar connection kinds (buttons btn-conn-{id} in ViperIDE.html). 'vm' and
 * 'rtc' are not toolbar buttons: they arrive through URLs or the menu. */
export const TOOLBAR_CONN_KINDS = [
    { id: 'ws',  labelKey: 'tool.conn.ws',  iconName: 'link',         needsGesture: false },
    { id: 'ble', labelKey: 'tool.conn.ble', iconName: 'bluetooth-b',  needsGesture: true  },
    { id: 'usb', labelKey: 'tool.conn.usb', iconName: 'usb',          needsGesture: true  },
]

export const AUTO_CONN_KINDS = [
    { id: 'vm',  labelKey: 'tool.conn.vm',  iconName: 'microchip',    needsGesture: false },
    { id: 'rtc', labelKey: 'tool.conn.rtc', iconName: 'link',         needsGesture: false },
]

/* Upstream default WebREPL relay hub; a self-hosted relay (see
 * src/websocket_relay.cjs) can replace this in the branding phase. */
export const RELAY_BASE = 'wss://hub.viper-ide.org/relay/'

export function allKinds() {
    return [...TOOLBAR_CONN_KINDS, ...AUTO_CONN_KINDS]
}

export function kindById(id) {
    return allKinds().find(k => k.id === id) || null
}

/*
 * Normalizes what a user typed as a WebREPL address, mirroring the prompt
 * handling in prepareNewPort(): tolerate http(s) prefixes and bare hosts.
 * Returns the websocket URL, or null when nothing usable was entered.
 */
export function normalizeWebreplUrl(input) {
    if (!input) { return null }
    let url = String(input).trim()
    if (url.startsWith('http://')) { url = url.slice(7) }
    if (url.startsWith('https://')) { url = url.slice(8) }
    if (!url.includes('://')) { url = 'ws://' + url }
    if (!url.startsWith('ws://') && !url.startsWith('wss://')) { return null }
    return url
}

/*
 * Under https, a plain ws:// target cannot be reached by the browser: the app
 * navigates to the device page instead, which reloads and asks for the
 * password there. Returns { redirect: 'http://…' } in that case, else null.
 */
export function httpsRedirect(wsUrl) {
    if (!wsUrl || !wsUrl.startsWith('ws://')) { return null }
    return { redirect: wsUrl.replace('ws://', 'http://') }
}

/* Classification of a ready-to-use connection string. */
export function classifyScheme(url) {
    if (url.startsWith('ws://') || url.startsWith('wss://')) { return 'ws' }
    if (url.startsWith('rtc://')) { return 'rtc' }
    if (url.startsWith('vm://')) { return 'vm' }
    return null
}

function tryParse(urlStr) {
    if (typeof URL.parse === 'function') { return URL.parse(urlStr) }
    try { return new URL(urlStr) } catch (_err) { return null }
}

/*
 * Blynk stream URLs (wss://<host>.blynk.cloud/stream/<32-char token>/<ds>)
 * are rewritten to the message-forwarder endpoint, as in app.js. Returns the
 * rewritten URL or null when the input is not such a stream.
 */
export function blynkRewrite(urlStr) {
    const info = tryParse(urlStr)
    if (!info) { return null }
    if (!info.host.includes('blynk') || !info.pathname.startsWith('/stream/')) { return null }
    const [, , token, ds] = info.pathname.split('/')
    if (!token || !/^[A-Za-z0-9\-_]{32}$/.test(token)) { return null }
    return `wss://${info.host}:443/msgforwarder?deviceToken=${token}&dataStreamName=${ds}`
}

/* Parses an rtc://… or relay wss://…/… connection id string to its raw id. */
export function parseConnectionIdUrl(urlStr) {
    let body = null
    if (urlStr.startsWith('rtc://')) { body = urlStr.slice(6) }
    else if (urlStr.startsWith(RELAY_BASE)) { body = urlStr.slice(RELAY_BASE.length) }
    if (!body) { return null }
    try { return ConnectionUID.parse(body).value() } catch (_err) { return null }
}

/*
 * URL parameters → auto-connect target, mirroring app.js startup handling:
 *   ?wss=<id>  ->  relay wss url      ?rtc=<id> -> rtc://<id>
 *   ?vm=<id>   ->  vm://<id>
 * `param(name)` behaves like URLSearchParams.get; returns null when absent
 * or malformed.
 */
export function autoConnectUrl(param) {
    const idParam = param('wss') || param('rtc') || param('vm')
    if (!idParam) { return null }
    try {
        const id = ConnectionUID.parse(idParam).value()
        if (param('wss')) { return RELAY_BASE + id }
        if (param('rtc')) { return 'rtc://' + id }
        return 'vm://' + id
    } catch (_err) {
        return null
    }
}

/*
 * Browser capability check for a connection kind, mirroring the guard block
 * in prepareNewPort(). env supplies the capability facts; returns
 * { ok: true } or { ok: false, why: <reason id> } where reason ids are:
 *   'no-ios'  WebBluetooth/WebSerial unavailable on iOS
 *   'no-secure'  API needs a secure context
 *   'no-ble'     navigator.bluetooth missing
 *   'no-serial'  neither navigator.serial nor navigator.usb present
 */
export function checkUsable(kind, env) {
    if (kind === 'ws' || kind === 'vm' || kind === 'rtc') { return { ok: true } }
    if (kind === 'ble' || kind === 'usb') {
        if (env.iOS) { return { ok: false, why: 'no-ios' } }
        if (!env.secure) { return { ok: false, why: 'no-secure' } }
    }
    if (kind === 'ble' && !env.bluetooth) { return { ok: false, why: 'no-ble' } }
    if (kind === 'usb' && !env.serial && !env.usb) { return { ok: false, why: 'no-serial' } }
    return { ok: true }
}
