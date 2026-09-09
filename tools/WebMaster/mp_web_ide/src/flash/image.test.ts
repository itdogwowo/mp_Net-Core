import { describe, expect, it } from 'vitest'

import { inspectImage, looksLikeFactoryImage, ESP_IMAGE_MAGIC } from './image.ts'

describe('flash/image', () => {

    it('detects an ESP image by its 0xE9 magic', () => {
        const img = new Uint8Array(0x5000)
        img[0] = ESP_IMAGE_MAGIC
        img[1] = 1   // segment count
        const r = inspectImage(img)
        expect(r.espImage).toBe(true)
        expect(r.note).toBe('esp-image')
        expect(r.size).toBe(0x5000)
    })

    it('treats short 0xE9 files as having an esp magic but too small', () => {
        const img = new Uint8Array(4)
        img[0] = ESP_IMAGE_MAGIC
        const r = inspectImage(img)
        expect(r.espImage).toBe(true)
        expect(r.note).toBe('esp-image-too-small')
    })

    it('flags non-ESP data and empty files', () => {
        expect(inspectImage(new Uint8Array([1, 2, 3, 4])).note).toBe('not-an-esp-image')
        expect(inspectImage(new Uint8Array(0)).note).toBe('empty')
        expect(inspectImage(new Uint8Array(0)).espImage).toBe(false)
    })
})

describe('flash/image factory detection', () => {

    it('detects MicroPython official image names', () => {
        expect(looksLikeFactoryImage('ESP32_GENERIC_S3-20260824-v1.29.0.bin', 100)).toBe(true)
        expect(looksLikeFactoryImage('ESP32_GENERIC-v1.28.0.bin', 200)).toBe(true)
        expect(looksLikeFactoryImage('my-micropython-fw.bin', 5000)).toBe(true)
    })

    it('detects by size for unnamed large files', () => {
        expect(looksLikeFactoryImage('firmware.bin', 1_800_000)).toBe(true)
        expect(looksLikeFactoryImage('app.bin', 500_000)).toBe(false)
    })

    it('leaves small plain app images alone', () => {
        expect(looksLikeFactoryImage('main-app.bin', 800_000)).toBe(false)
    })
})
