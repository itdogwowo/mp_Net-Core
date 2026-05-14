def main():
    from lib.bootstrap import build_bus

    bus = build_bus()
    from app import App
    app = App()
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
