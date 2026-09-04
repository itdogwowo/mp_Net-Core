class BusSources:
    def __init__(self):
        self._lst = []
        self._ids = set()

    def add(self, b):
        if b is None:
            return False
        bid = id(b)
        if bid in self._ids:
            return False
        self._lst.append(b)
        self._ids.add(bid)
        return True

    def remove(self, b):
        if b is None:
            return False
        bid = id(b)
        if bid not in self._ids:
            return False
        self._ids.remove(bid)
        for i in range(len(self._lst)):
            if id(self._lst[i]) == bid:
                self._lst.pop(i)
                break
        return True

    def clear(self):
        self._lst = []
        self._ids = set()

    def list(self):
        return self._lst
