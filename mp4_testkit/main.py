def main():
    from lib.bootstrap import build_bus

    bus = build_bus()
    from app import App
    app = App()
    try:
        from lib.network_manager import NetworkManager
        nm = bus.get_service("network_manager")
        if nm is None:
            nm = NetworkManager(bus)
            bus.set_service("network_manager", nm)
        nm.init_from_config()
    except Exception:
        pass
    try:
        from lib.web_ui import WebUIService
        web_cfg = bus.shared.get("Web", {}) or {}
        port = int(web_cfg.get("port", 80) or 80)
        root = web_cfg.get("root", "web") or "web"
        svc = bus.get_service("web_ui")
        if svc is None:
            svc = WebUIService(port=port, web_root=root)
            bus.set_service("web_ui", svc)
        svc.set_app(app)
        if int(web_cfg.get("enable", 0) or 0):
            svc.enable()
        else:
            svc.disable()
    except Exception:
        pass
    comm = bus.get_service("comm")
    if comm:
        def _on_packet(ver, addr, cmd, payload, b, _comm):
            ctx = {
                "app": app,
                "transport": getattr(b, "label", "Bus"),
                "send": getattr(b, "write", None),
                "ver": int(ver),
                "addr": int(addr),
                "cmd": int(cmd),
            }
            app.disp.dispatch(int(cmd) & 0xFFFF, payload, ctx)
        comm.on_packet(_on_packet)

    import _thread
    from Core1_engine import task_loop as core1_loop

    _thread.start_new_thread(core1_loop, (bus,))

    from Core0_worker import task_loop as core0_loop

    core0_loop(bus)


if __name__ == "__main__":
    main()
