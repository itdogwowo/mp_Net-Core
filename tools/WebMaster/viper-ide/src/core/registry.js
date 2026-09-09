/*
 * SPDX-FileCopyrightText: 2026 mp_Net-Core
 * SPDX-License-Identifier: MIT
 *
 * Registration tables for commands, settings, tools, transports and side
 * tabs. UI (toolbar, settings panel, side menu) is later rendered from these
 * tables; feature modules declare themselves here instead of wiring DOM ids.
 * Zero dependencies, no DOM.
 */

export function createRegistry() {
    const commands = new Map()
    const settings = new Map()
    const tools = new Map()
    const transports = new Map()
    const sideTabs = new Map()

    function claim(map, id, def) {
        if (!id || typeof id !== 'string') {
            throw new Error(`registry: id must be a non-empty string, got ${String(id)}`)
        }
        if (map.has(id)) {
            throw new Error(`registry: duplicate id '${id}'`)
        }
        map.set(id, { ...def, id })
        return map.get(id)
    }

    return {
        command:     (id, def) => claim(commands, id, def),
        setting:     (id, def) => claim(settings, id, def),
        tool:        (id, def) => claim(tools, id, def),
        transport:   (id, def) => claim(transports, id, def),
        sideTab:     (id, def) => claim(sideTabs, id, def),

        getCommand:   (id) => commands.get(id),
        getSetting:   (id) => settings.get(id),
        getTool:      (id) => tools.get(id),
        getTransport: (id) => transports.get(id),

        listCommands:   () => [...commands.values()],
        listSettings:   () => [...settings.values()],
        listTools:      () => [...tools.values()],
        listTransports: () => [...transports.values()],
        listSideTabs:   () => [...sideTabs.values()],
    }
}

export default createRegistry
