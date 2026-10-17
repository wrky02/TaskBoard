import os

class InstanceLock:
    def __init__(self, root):
        self.path = root / 'instance.lock'
        self.file = None
    def acquire(self):
        self.file = self.path.open('a+b')
        self.file.seek(0)
        # Windows byte-range locks also block reads by another instance.
        # Inspect file size instead of reading the locked byte.
        if os.fstat(self.file.fileno()).st_size == 0: self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close(); self.file = None
            return False
    def close(self):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close(); self.file = None

