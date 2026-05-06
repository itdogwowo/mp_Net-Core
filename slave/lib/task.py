class Task:
    def __init__(self, name, ctx):
        self.name = name
        self.ctx = ctx
        self.running = False
        self.run_once = False # If True, TaskManager will stop it after one loop
        
    def on_start(self):
        """Called when the task is scheduled on a core."""
        self.running = True
        # print(f"Task [{self.name}] starting...")

    def loop(self):
        """The main loop body. Must be non-blocking."""
        pass

    def on_stop(self):
        """Called when the task is unscheduled."""
        self.running = False
        # print(f"Task [{self.name}] stopping...")
