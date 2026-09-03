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
