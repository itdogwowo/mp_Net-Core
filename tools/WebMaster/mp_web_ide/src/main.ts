/* mp_web_ide — 燒錄三步 UI 主邏輯 */

import './style.css'

import { inspectImage } from './flash/image.ts'
import {
    suggestAddress, parseHexAddress, formatHexAddress, FALLBACK_APP_OFFSET,
} from './flash/addresses.ts'
import { FlashSession, isFlashSupported } from './flash/esptool.ts'

/* ---------- DOM ---------- */
const $ = <T extends HTMLElement>(id: string): T => {
    const el = document.getElementById(id)
    if (!el) { throw new Error(`missing element #${id}`) }
    return el as T
}

const envNotice = $('envNotice')
const dropZone = $('dropZone')
const pickBtn = $<HTMLButtonElement>('pickBtn')
const fileInput = $<HTMLInputElement>('fileInput')
const fileInfo = $('fileInfo')
const connectBtn = $<HTMLButtonElement>('connectBtn')
const releaseBtn = $<HTMLButtonElement>('releaseBtn')
const deviceInfo = $('deviceInfo')
const devChip = $('devChip')
const devDesc = $('devDesc')
const devMac = $('devMac')
const devFlash = $('devFlash')
const connectHint = $('connectHint')
const addressKind = $<HTMLSelectElement>('addressKind')
const addressInput = $<HTMLInputElement>('addressInput')
const addressNote = $('addressNote')
const eraseAll = $<HTMLInputElement>('eraseAll')
const flashBtn = $<HTMLButtonElement>('flashBtn')
const progressWrap = $('progressWrap')
const progressBar = $('progressBar')
const progressText = $('progressText')
const statusLine = $('statusLine')
const logSection = $('logSection')
const logBox = $('logBox')

/* ---------- 狀態 ---------- */
let fileData: Uint8Array | null = null
let fileName = ''
let session: FlashSession | null = null
let chipName: string | null = null
let detectedFlashSize = ''
let busy = false

/* ---------- 小工具 ---------- */
function setStatus(text: string, kind: 'ok' | 'err' | 'info' = 'info'): void {
    statusLine.textContent = text
    statusLine.className = `status ${kind}`
}

function log(data: string): void {
    logSection.classList.remove('hidden')
    logBox.textContent += data
    logBox.scrollTop = logBox.scrollHeight
}

function logLine(data: string): void {
    log(data + '\n')
}

function fmtSize(bytes: number): string {
    if (bytes < 1024) { return `${bytes} B` }
    if (bytes < 1024 * 1024) { return `${(bytes / 1024).toFixed(1)} KB` }
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function syncBusyState(): void {
    pickBtn.disabled = busy
    // 連線不綁檔案：可以先連線偵測晶片，再選 .bin（也可反過來）
    connectBtn.disabled = busy
    flashBtn.disabled = busy || !fileData || !session
}

/* ---------- ① 選檔 ---------- */
async function loadFile(file: File): Promise<void> {
    if (busy) { return }
    try {
        const buf = await file.arrayBuffer()
        const data = new Uint8Array(buf)
        const insp = inspectImage(data)
        fileData = data
        fileName = file.name

        let text = `📄 ${fileName} · ${fmtSize(data.byteLength)}`
        if (!insp.espImage) {
            text += ' · ⚠️ 檔首不是 ESP 映像 (0xE9)，可能不是正確的韌體'
            fileInfo.className = 'file-info warn'
        } else if (insp.note === 'esp-image-too-small') {
            text += ' · ⚠️ 檔案過小，看起來不完整'
            fileInfo.className = 'file-info warn'
        } else {
            text += ' · ✅ 偵測到 ESP 映像檔頭'
            fileInfo.className = 'file-info ok'
        }
        fileInfo.textContent = text
        applyAddress()
        syncBusyState()
        setStatus('')
    } catch (err) {
        fileData = null
        setStatus(`讀檔失敗：${String(err)}`, 'err')
        syncBusyState()
    }
}

function wireFilePicker(): void {
    dropZone.addEventListener('click', () => fileInput.click())
    dropZone.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fileInput.click() }
    })
    dropZone.addEventListener('dragover', (ev) => {
        ev.preventDefault()
        dropZone.classList.add('over')
    })
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('over'))
    dropZone.addEventListener('drop', (ev) => {
        ev.preventDefault()
        dropZone.classList.remove('over')
        const file = ev.dataTransfer?.files?.[0]
        if (file) { void loadFile(file) }
    })
    pickBtn.addEventListener('click', (ev) => { ev.stopPropagation(); fileInput.click() })
    fileInput.addEventListener('change', () => {
        const file = fileInput.files?.[0]
        if (file) { void loadFile(file) }
    })
}

/* ---------- ② 位址建議（自動化核心） ---------- */
function currentAddressKind(): 'app' | 'merged' | 'custom' {
    const v = addressKind.value
    return v === 'app' || v === 'merged' || v === 'custom' ? v : 'app'
}

function applyAddress(): void {
    const kind = currentAddressKind()
    if (kind === 'app') {
        const addr = suggestAddress(chipName, 'app')
        addressInput.value = formatHexAddress(addr)
        addressInput.readOnly = true
        addressNote.textContent = chipName
            ? `依偵測晶片 ${chipName} 自動帶入`
            : '連線偵測晶片後會依晶片自動帶入'
    } else if (kind === 'merged') {
        addressInput.value = formatHexAddress(0)
        addressInput.readOnly = true
        addressNote.textContent = '整包韌體（含 bootloader 等）固定寫在 0x0'
    } else {
        addressInput.readOnly = false
        addressNote.textContent = '進階：請確認位址正確'
    }
}

function resolveAddress(): number {
    const kind = currentAddressKind()
    if (kind === 'app') { return suggestAddress(chipName, 'app') }
    if (kind === 'merged') { return 0 }
    const parsed = parseHexAddress(addressInput.value)
    if (parsed === null) {
        throw new Error(`位址格式錯誤：${addressInput.value}（請填 hex，如 0x10000）`)
    }
    return parsed
}

/* ---------- ② 連線 ---------- */
/* 釋放本頁持有的序列埠（含失敗殘留）；回傳是否曾有 session。 */
async function releaseSession(): Promise<boolean> {
    const s = session
    session = null
    chipName = null
    deviceInfo.classList.add('hidden')
    if (!s) { return false }
    try { await s.close() } catch { /* 舊埠可能已死，忽略 */ }
    return true
}

async function onConnect(): Promise<void> {
    if (busy) { return }
    busy = true
    syncBusyState()
    setStatus('')
    logBox.textContent = ''   // 新一輪連線：清空上次記錄，避免混淆

    // 釋放上一次的 session（失敗後重連不用 F5：先關舊埠再開新的）
    await releaseSession()

    try {
        // 不過濾 VID/PID：萬一有非清單內嘅轉接晶片都揀得到
        const port = await navigator.serial.requestPort()
        session = new FlashSession(port, {
            write: (d) => log(d),
            writeLine: (d) => logLine(d),
            clean: () => { logBox.textContent = '' },
        })
        connectBtn.textContent = '偵測中…'
        const info = await session.detect()
        chipName = session.chipName ?? info.chipName

        devChip.textContent = info.chipName
        devDesc.textContent = info.chipDescription ?? '—'
        devMac.textContent = info.mac ?? '—'
        detectedFlashSize = info.flashSize ?? '—'
        devFlash.textContent = detectedFlashSize
        deviceInfo.classList.remove('hidden')

        connectHint.textContent = ''
        applyAddress()
        setStatus(`✅ 已偵測到 ${info.chipName}${info.flashSize ? `（Flash ${info.flashSize}）` : ''}，可以燒錄了`, 'ok')
    } catch (err) {
        // 釋放可能已開啟的序列埠（偵測中失敗也會留下 open 的埠）
        const s = session
        session = null
        chipName = null
        if (s) { void s.close() }
        deviceInfo.classList.add('hidden')

        const msg = err instanceof Error ? err.message : String(err)
        if (err instanceof DOMException && err.name === 'NotFoundError') {
            setStatus('已取消選擇裝置', 'info')
        } else if (/already open/i.test(msg)) {
            connectHint.textContent = ''
            setStatus('連線失敗：序列埠正被占用', 'err')
            logLine('⚠️ 序列埠已經被打開。WebSerial 同一時間只允許一個頁面占用：')
            logLine('  • 檢查其他分頁：官方 esptool demo（espressif.github.io）、ViperIDE、')
            logLine('    或本頁舊分頁，只要還連著裝置就要先關閉/斷開')
            logLine('  • 關閉後直接再按「連線」即可，不需要重新整理頁面')
            logLine('  • 也可以按「釋放序列埠」後再試')
        } else {
            setStatus(`連線失敗：${msg}`, 'err')
            logLine('⚠️ 連線失敗：' + msg)
            logLine('提示：若晶片沒回應，可按住 BOOT 再重新插 USB 後重試')
        }
    } finally {
        busy = false
        connectBtn.textContent = '重新選擇裝置並連線'
        syncBusyState()
    }
}

/* ---------- ③ 燒錄 ---------- */
async function onFlash(): Promise<void> {
    if (busy || !session || !fileData) { return }
    const data = fileData
    let address: number
    try {
        address = resolveAddress()
    } catch (err) {
        setStatus(err instanceof Error ? err.message : String(err), 'err')
        return
    }

    busy = true
    syncBusyState()
    progressWrap.classList.remove('hidden')
    progressBar.style.width = '0%'
    progressText.textContent = '準備中…'
    flashBtn.textContent = '燒錄中…'
    setStatus('')

    let succeeded = false
    try {
        await session.flash(
            [{ data, address }],
            {
                eraseAll: eraseAll.checked,
                compress: true,
                onProgress: (written, total) => {
                    const pct = total > 0 ? Math.round((written / total) * 100) : 0
                    progressBar.style.width = `${pct}%`
                    progressText.textContent = `${pct}% · ${fmtSize(written)} / ${fmtSize(total)}`
                },
            },
        )
        progressBar.style.width = '100%'
        progressText.textContent = '完成'
        succeeded = true
        setStatus(`✅ 燒錄完成！韌體已寫入 ${formatHexAddress(address)}，裝置已重新啟動`, 'ok')
    } catch (err) {
        setStatus(`燒錄失敗：${err instanceof Error ? err.message : String(err)}`, 'err')
        logLine('⚠️ 燒錄失敗，可重插 USB 後再試一次')
    } finally {
        busy = false
        flashBtn.textContent = succeeded ? '燒錄完成（可再燒一次）' : '開始燒錄'
        syncBusyState()
        // 釋放序列埠；裝置資訊保留在畫面上供參考
        const s = session
        session = null
        chipName = null
        applyAddress()
        if (s) { void s.close() }
    }
}

/* ---------- 初始 ---------- */
function initEnvNotice(): void {
    const hasSerial = typeof navigator !== 'undefined' && typeof navigator.serial !== 'undefined'
    const secure = window.isSecureContext ||
        location.hostname === 'localhost' || location.hostname === '127.0.0.1'
    if (hasSerial && secure) {
        envNotice.classList.add('hidden')
        return
    }
    envNotice.classList.remove('hidden')
    const reasons: string[] = []
    if (!hasSerial) { reasons.push('此瀏覽器沒有 WebSerial 支援（請用 Chrome/Edge 112+）') }
    if (!secure) { reasons.push('需要安全來源（https 或 localhost/127.0.0.1）才能使用 WebSerial') }
    envNotice.textContent = '⚠️ 無法燒錄：' + reasons.join('；')
    connectBtn.disabled = true
}

wireFilePicker()
addressKind.addEventListener('change', applyAddress)
connectBtn.addEventListener('click', () => void onConnect())
releaseBtn.addEventListener('click', () => void (async () => {
    if (busy) { return }
    const had = await releaseSession()
    setStatus(had ? '已釋放序列埠，可以重新連線' : '目前沒有占用中的序列埠（由本頁持有的）', 'info')
    syncBusyState()
})())
flashBtn.addEventListener('click', () => void onFlash())

// 頁面關閉/重整前自動釋放，減少「殘留占用」需要 F5 才能解的情況
window.addEventListener('beforeunload', () => {
    const s = session
    session = null
    if (s) { void s.close() }
})

// 未連線前先顯示通用建議位址
addressInput.value = formatHexAddress(FALLBACK_APP_OFFSET)
applyAddress()
initEnvNotice()
syncBusyState()
