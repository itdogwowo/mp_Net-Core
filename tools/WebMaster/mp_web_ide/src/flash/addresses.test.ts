import { describe, expect, it } from 'vitest'

import {
    MERGED_OFFSET, APP_OFFSET_BY_CHIP, FALLBACK_APP_OFFSET,
    appOffsetForChip, suggestAddress, parseHexAddress, formatHexAddress,
} from './addresses.ts'

describe('flash/addresses', () => {

    it('maps known chips to their app offsets', () => {
        expect(appOffsetForChip('ESP32-S3')).toBe(0x10000)
        expect(appOffsetForChip('ESP32')).toBe(0x10000)
        expect(appOffsetForChip('ESP8266')).toBe(0x0)
        expect(appOffsetForChip('esp32-c6')).toBe(0x10000)   // case-insensitive
    })

    it('falls back for unknown chips and missing names', () => {
        expect(appOffsetForChip('ESP99-XYZ')).toBe(FALLBACK_APP_OFFSET)
        expect(appOffsetForChip(null)).toBe(FALLBACK_APP_OFFSET)
        expect(appOffsetForChip(undefined)).toBe(FALLBACK_APP_OFFSET)
    })

    it('merged images always go to 0x0', () => {
        expect(suggestAddress('ESP32-S3', 'merged')).toBe(MERGED_OFFSET)
        expect(suggestAddress(null, 'merged')).toBe(MERGED_OFFSET)
        expect(suggestAddress('ESP32', 'app')).toBe(0x10000)
        expect(suggestAddress(null, 'unknown')).toBe(FALLBACK_APP_OFFSET)
    })

    it('table contains no duplicate offsets for the S2/S3/C3 family assumptions', () => {
        // 檢查已知慣例組都落在 0x10000（這組是 esp-idf 最常見預設）
        for (const chip of ['ESP32', 'ESP32-S2', 'ESP32-S3', 'ESP32-C3', 'ESP32-C6']) {
            expect(APP_OFFSET_BY_CHIP[chip], chip).toBe(0x10000)
        }
    })

    it('parses hex addresses with or without prefix', () => {
        expect(parseHexAddress('0x10000')).toBe(0x10000)
        expect(parseHexAddress('10000')).toBe(0x10000)
        expect(parseHexAddress('0X8000')).toBe(0x8000)
        expect(parseHexAddress(' 0x0 ')).toBe(0x0)
    })

    it('rejects malformed or out-of-range addresses', () => {
        expect(parseHexAddress('')).toBeNull()
        expect(parseHexAddress('0x')).toBeNull()
        expect(parseHexAddress('12G4')).toBeNull()
        expect(parseHexAddress('0x100000000')).toBeNull()   // > 32-bit
        expect(parseHexAddress('nonsense')).toBeNull()
    })

    it('formats addresses back to hex', () => {
        expect(formatHexAddress(0x10000)).toBe('0x10000')
        expect(formatHexAddress(0)).toBe('0x0')
    })
})
