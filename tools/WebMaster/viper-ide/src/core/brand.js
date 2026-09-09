/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Single place to re-brand the product shell. Feature code reads these fields
 * instead of hard-coding the upstream name. All features stay enabled during
 * the modularization phase; trimming (telemetry, unused tools) happens later.
 */

import { config } from './config.js'

export const brand = {
    appName: 'ViperIDE',
    appVersion: config.version,
    logoPath: 'assets/logo_1024.png',
    telemetryEnabled: true,   // upstream behaviour, kept until the trimming phase
}

export default brand
