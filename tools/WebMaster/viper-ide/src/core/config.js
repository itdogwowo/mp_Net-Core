/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Build-time environment. rollup (@rollup/plugin-replace, see rollup.config.mjs)
 * substitutes the VIPER_IDE_* identifiers; when loaded outside a bundle
 * (unit tests, Node) the typeof guards fall back to dev defaults.
 */

export const config = {
    version: (typeof VIPER_IDE_VERSION !== 'undefined') ? VIPER_IDE_VERSION : 'dev',
    build:   (typeof VIPER_IDE_BUILD !== 'undefined') ? VIPER_IDE_BUILD : null,
    baseUrl: (typeof VIPER_IDE_BASE_URL !== 'undefined') ? VIPER_IDE_BASE_URL : './',
}

export default config
