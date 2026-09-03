/*
 * mp_web_ide — flash: 位址建議與解析（純邏輯，可測）
 *
 * 動機（見 docs/specs/flash-ui.md）：新手不必記位址；依偵測到的晶片自動帶
 * 預設 app 偏移。位址表以 esp-idf 預設分割區為準；標「待核」者 M1 以文件核對。
 *
 * 授權註記：esptool-js 為 Apache-2.0（非 MIT）。
 */

export const MERGED_OFFSET = 0x0          // 整包 merged（bootloader+pt+app）一律 0x0

/** 單一 app 映像的預設 flash 偏移（esp-idf 預設分割區 app 起點）。 */
export const APP_OFFSET_BY_CHIP: Readonly<Record<string, number>> = {
    'ESP8266':  0x00000,   // 8266 無二段式時 app 直接 0x0（整包亦 0x0）
    'ESP32':    0x10000,
    'ESP32-S2': 0x10000,
    'ESP32-S3': 0x10000,
    'ESP32-C3': 0x10000,
    'ESP32-C2': 0x10000,   // 待核（沿用 C3 慣例）
    'ESP32-C6': 0x10000,
    'ESP32-H2': 0x10000,
    'ESP32-C5': 0x10000,   // 待核（新晶片，沿用慣例）
    'ESP32-P4': 0x20000,   // 待核（P4 分割慣例不同，M1 需以文件確認）
}

export const FALLBACK_APP_OFFSET = 0x10000

export type ImageKind = 'app' | 'merged' | 'unknown'

/**
 * 依晶片名（esptool loader.chip 的 CHIP_NAME，大小寫不拘）找預設 app 偏移。
 * 找不到時回傳 FALLBACK（0x10000），由 UI 顯示「依晶片預設」。
 */
export function appOffsetForChip(chipName: string | null | undefined): number {
    if (!chipName) { return FALLBACK_APP_OFFSET }
    const name = chipName.trim().toUpperCase()
    for (const [key, offset] of Object.entries(APP_OFFSET_BY_CHIP)) {
        if (name === key || name.startsWith(key)) { return offset }
    }
    return FALLBACK_APP_OFFSET
}

/** 綜合建議：merged 一律 0x0；否則依晶片。 */
export function suggestAddress(chipName: string | null | undefined, kind: ImageKind): number {
    if (kind === 'merged') { return MERGED_OFFSET }
    return appOffsetForChip(chipName)
}

/** 解析使用者輸入的位址（容許 0x 前綴、純 hex、小寫）；失敗回 null。 */
export function parseHexAddress(input: string): number | null {
    const s = String(input).trim()
    if (!/^(0[xX])?[0-9a-fA-F]{1,8}$/.test(s)) { return null }
    const n = Number.parseInt(s.replace(/^0[xX]/, ''), 16)
    if (!Number.isSafeInteger(n) || n < 0 || n > 0xFFFFFFFF) { return null }
    return n
}

export function formatHexAddress(n: number): string {
    return '0x' + n.toString(16)
}
