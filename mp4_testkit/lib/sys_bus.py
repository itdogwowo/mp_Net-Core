class SysBus:
    def __init__(self):
        self.shared = {}
        self.services = {}

    def set_service(self, name, value):
        self.services[name] = value

    def get_service(self, name, default=None):
        return self.services.get(name, default)
