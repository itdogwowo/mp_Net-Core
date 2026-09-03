import { describe, expect, it } from 'vitest'

import { inspectImage, ESP_IMAGE_MAGIC } from './image.ts'

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
