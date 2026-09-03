/*
 * mp_web_ide — esptool-js 瀏覽器封裝
 *
 * 把 WebSerial port → 偵測 → 燒錄 → 結束 的流程包成一個可拋棄的 session；
 * UI 只跟這個檔案往來。API 以 esptool-js 0.6.1 的型別為準（Apache-2.0）。
 */

import { ESPLoader, Transport, type FlashOptions, type FlashModeValues, type FlashFreqValues, type FlashSizeValues } from 'esptool-js'

export interface LogSink {
    write(data: string): void
    writeLine(data: string): void
    clean(): void
}

export interface FlashTarget {
    data: Uint8Array
    address: number
}

export interface DetectedChip {
    chipName: string
    chipDescription?: string
    mac?: string
    flashSize?: string
}

export interface FlashRunOptions {
    eraseAll: boolean
    compress: boolean
    onProgress: (written: number, total: number) => void
}

export function isFlashSupported(): boolean {
    return typeof navigator !== 'undefined' &&
        typeof navigator.serial !== 'undefined' &&
        (window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1')
}

/** esptool 要求的 terminal 介面（{clean,write,writeLine}），接到我們的 log。 */
export function makeTerminal(log: LogSink): { clean(): void; write(data: string): void; writeLine(data: string): void } {
    return {
        clean: () => log.clean(),
        write: (data) => log.write(data),
        writeLine: (data) => log.writeLine(data),
    }
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 硬關閉一個 WebSerial 埠（忽略所有錯誤）：
 * cancel reader → abort writer → 輪詢直到 readable/writable 都釋放 → close()。
 * 用於清掉本頁任何殘留狀態（失敗嘗試、舊 session…）；若埠由「其他 origin」
 * 持有，close 會失敗——屬預期，唯一解法係關閉嗰個頁面。
 */
export async function closePortHard(port: SerialPort): Promise<void> {
    try {
        if (port.readable) {
            const reader = port.readable.getReader()
            try { await reader.cancel() } catch { /* ignore */ }
            try { reader.releaseLock() } catch { /* ignore */ }
        }
    } catch { /* ignore */ }
    try {
        if (port.writable) {
            const w = port.writable
            try { await w.abort() } catch { /* ignore */ }
        }
    } catch { /* ignore */ }

    // 輪詢等 stream 釋放（close() 要求兩者皆 null）
    const deadline = Date.now() + 2000
    while (Date.now() < deadline && (port.readable !== null || port.writable !== null)) {
        await sleep(100)
    }

    try {
        if (port.readable === null && port.writable === null) {
            await port.close()
        }
    } catch { /* 其他 origin 持有或已關閉，忽略 */ }
}

export class FlashSession {
    private transport: Transport | null = null
    private loader: ESPLoader | null = null

    constructor(
        private readonly port: SerialPort,
        private readonly log: LogSink,
    ) {}

    /** 連線並偵測晶片；回傳可顯示的晶片資訊。 */
    async detect(): Promise<DetectedChip> {
        // 自癒：先確保埠係關住（清本頁殘留）。其他 origin 持有時 close 會失敗，
        // 之後 loader.connect 的 open 會拋 already open → 由 UI 提示關閉該頁。
        await closePortHard(this.port)

        this.transport = new Transport(this.port, false)

        // ⚠️ 唔好喺度手動 transport.connect()：ESPLoader.connect()（main() 內部）
        // 自己會 open 個埠，先開一次會撞 "The port is already open"。
        this.loader = new ESPLoader({
            transport: this.transport,
            baudrate: 921600,
            terminal: makeTerminal(this.log),
        })

        const chipName = await this.loader.main()
        const chip = this.loader.chip as unknown as {
            CHIP_NAME?: string
            getChipDescription?: (loader: unknown) => Promise<string>
            readMac?: (loader: unknown) => Promise<string>
        }

        const desc = await safeChipCall(async () => chip.getChipDescription?.(this.loader))
        const mac = await safeChipCall(async () => chip.readMac?.(this.loader))
        let flashSize: string | undefined
        try { flashSize = await this.loader.detectFlashSize() } catch { /* 某些 ROM 不支援，忽略 */ }

        return {
            chipName: chipName || chip.CHIP_NAME || 'unknown',
            chipDescription: desc,
            mac,
            flashSize,
        }
    }

    /** 依偵測結果給建議位址用：晶片名（可能為 null）。 */
    get chipName(): string | null {
        return this.loader?.chip.CHIP_NAME ?? null
    }

    /** 執行燒錄；成功後 hard reset 讓韌體跑起來。 */
    async flash(targets: FlashTarget[], opts: FlashRunOptions): Promise<void> {
        if (!this.loader) { throw new Error('尚未連線') }

        const total = targets.reduce((sum, t) => sum + t.data.byteLength, 0)
        const flashOptions: FlashOptions = {
            fileArray: targets,
            flashMode: 'keep' as FlashModeValues,
            flashFreq: 'keep' as FlashFreqValues,
            flashSize: 'keep' as FlashSizeValues,
            eraseAll: opts.eraseAll,
            compress: opts.compress,
            reportProgress: (_fileIndex: number, written: number, _total: number) => {
                opts.onProgress(written, total)
            },
        }
        await this.loader.writeFlash(flashOptions)
        await this.loader.after('hard_reset')
    }

    /** 釋放序列埠；先 disconnect 再硬關，總計最多等約 3 秒。 */
    async close(): Promise<void> {
        const t = this.transport
        const port = this.port
        this.transport = null
        this.loader = null
        if (t) {
            try {
                await Promise.race([
                    t.disconnect(),
                    new Promise((resolve) => setTimeout(resolve, 1500)),
                ])
            } catch { /* 已斷線就算了 */ }
        }
        await closePortHard(port)
    }
}

async function safeChipCall<T>(fn: (() => Promise<T | undefined>) | undefined): Promise<T | undefined> {
    if (!fn) { return undefined }
    try { return await fn() } catch { return undefined }
}
