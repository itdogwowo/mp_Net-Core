/*
 * mp_web_ide — flash: 映像檔基本偵測（純邏輯，可測）
 *
 * 只做「不碰 esptool 也能說」的事：ESP 映像 magic、大小、粗略分類。
 * 深度解析（segment、checksum、整包 vs 單一 app）M1 依 esptool-js 的
 * image parser 再補。
 */

export const ESP_IMAGE_MAGIC = 0xE9

export interface ImageInspection {
    size: number
    magic: number | null
    espImage: boolean
    note: string
}

/**
 * 粗略判斷「似唔似 MicroPython 官方整包 .bin」（bootloader+app 合埋，寫 0x0）：
 * - 檔名含 ESP32_GENERIC / micropython，或
 * - 檔名係 ESP32 vX.Y.Z.bin 格式，或
 * - 檔案 >= 1.4MB（官方整包通常 1.5MB 以上）
 * 只係「建議用」，永遠可以人手改；app-only 大檔會誤判，UI 有顯示可改。
 */
export function looksLikeFactoryImage(fileName: string, size: number): boolean {
    const n = fileName.toLowerCase()
    if (/micropython/.test(n)) { return true }
    if (n.includes('esp32') && /generic/.test(n)) { return true }
    if (n.includes('esp32') && /v\d+\.\d+(\.\d+)?\.bin/.test(n)) { return true }
    return size >= 1_400_000
}

export function inspectImage(data: Uint8Array): ImageInspection {
    const size = data.byteLength
    const magic = size > 0 ? data[0] ?? null : null
    const espImage = magic === ESP_IMAGE_MAGIC
    let note: string
    if (!espImage) {
        note = size === 0
            ? 'empty'
            : 'not-an-esp-image'
    } else if (size < 8) {
        note = 'esp-image-too-small'      // 有 magic 但連基本 header 都不足
    } else if (size < 0x4000) {
        note = 'esp-image-small'          // 太小，不太像完整 app
    } else {
        note = 'esp-image'
    }
    return { size, magic, espImage, note }
}
