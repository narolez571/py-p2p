import heapq

class MRQ(object):
    '''
    A most-recently-seen queue.

    Keeps track of the N most-recently-seen objects, with quick
    member testing.
    '''
    def __init__(self, limit=50):
        self.limit = limit
        self.count = 0
        self.list = []
        self.set = set()

    def add(self, obj):
        if obj in self.set:
            # this is slow...
            idx = self.list.index(obj)
            self.list = self.list[:idx] + self.list[idx+1:]
            self.list.append(obj)
            return
        self.list.append(obj)
        self.set.add(obj)
        self.count += 1

    def remove(self):
        o = self.list[0]
        self.list = self.list[1:]
        self.set.remove(o)
        self.count -= 1        

    def __iadd__(self, obj):
        self.add(obj)
        while self.count > self.limit:
            self.remove()
        return self

    def __contains__(self, obj):
        return obj in self.set

    def __iter__(self):
        return self.list.__iter__()