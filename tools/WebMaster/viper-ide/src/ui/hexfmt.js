/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Pure formatting helpers for the hex viewer, extracted verbatim from
 * app.js hexViewer() (v0.6.5) so the byte->line logic is unit-testable
 * and the DOM code only renders ready-made rows.
 */

export function toHex(n) {
    return ('00' + n.toString(16)).slice(-2)
}

export function toPrintableAscii(n) {
    return (n >= 32 && n <= 126) ? String.fromCharCode(n) : '.'
}

/*
 * Formats one 16-byte row starting at `offset` of `bytes`, mirroring the
 * original app.js loop exactly: an extra space after the 8th byte, three
 * spaces for a missing byte in the hex column, and the trailing separator
 * trimmed. Returns { address, hex, ascii } ready for the DOM.
 */
export function hexLineParts(bytes, offset, rowSize = 16) {
    let hexPart = ''
    let asciiPart = ''

    for (let i = 0; i < rowSize; i++) {
        const at = offset + i
        if (at < bytes.length) {
            const b = bytes[at]
            hexPart += toHex(b) + ' '
            asciiPart += toPrintableAscii(b)
        } else {
            hexPart += '   '
            asciiPart += ' '
        }
        if (i === 7) { hexPart += ' ' }
    }

    return {
        address: offset.toString(16).padStart(8, '0'),
        hex: hexPart.slice(0, -1),
        ascii: asciiPart,
    }
}
