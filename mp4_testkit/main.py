def main():
    from lib.bootstrap import build_bus

    bus = build_bus()

    import _thread
    from Core1_engine import task_loop as core1_loop

    _thread.start_new_thread(core1_loop, (bus,))

    from Core0_worker import task_loop as core0_loop

    core0_loop(bus)


if __name__ == "__main__":
    main()
