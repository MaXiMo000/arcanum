"""An in-memory file system that simulates power loss, for crash-recovery tests.

Every file has a durable image (what survived the last sync) and a list of
pending writes since then. After arm(n), the n-th file operation raises
PowerLoss before it happens. power_loss() then decides what reached the
disk: each pending write survives or vanishes independently (disks reorder
unsynced writes), and a surviving write may be torn, keeping only its first
few 512-byte sectors.
"""

SECTOR = 512


class PowerLoss(BaseException):
    """Raised at the crash point. A BaseException so no engine code can swallow it."""


class _Image:
    def __init__(self):
        self.current = bytearray()  # what the running process sees
        self.durable = bytearray()  # what is guaranteed to be on disk
        self.pending = []  # ("write", offset, data) / ("truncate", size, None) since the last sync


def _write(buf, offset, data):
    if offset > len(buf):
        buf.extend(bytes(offset - len(buf)))
    buf[offset:offset + len(data)] = data


class CrashFile:
    def __init__(self, fs, image):
        self.fs = fs
        self.image = image

    def size(self):
        return len(self.image.current)

    def read(self, offset, size):
        return bytes(self.image.current[offset:offset + size])

    def write(self, offset, data):
        self.fs.tick()
        _write(self.image.current, offset, data)
        self.image.pending.append(("write", offset, bytes(data)))

    def truncate(self, size):
        self.fs.tick()
        del self.image.current[size:]
        self.image.pending.append(("truncate", size, None))

    def sync(self):
        self.fs.tick()
        self.image.durable = bytearray(self.image.current)
        self.image.pending = []

    def close(self):
        pass


class CrashFS:
    def __init__(self):
        self.files = {}
        self.operations = 0
        self._countdown = None

    def open(self, path):
        return CrashFile(self, self.files.setdefault(path, _Image()))

    def remove(self, path):
        self.tick()
        self.files.pop(path, None)

    def arm(self, operations):
        """Lose power just before the given number of further file operations."""
        self._countdown = operations

    def tick(self):
        self.operations += 1
        if self._countdown is not None:
            self._countdown -= 1
            if self._countdown <= 0:
                raise PowerLoss()

    def power_loss(self, rng, keep_everything=False):
        """Decide what reached the disk, then let the files be reopened.

        keep_everything=True models a killed process (the OS still writes its buffers).
        """
        self._countdown = None
        for image in self.files.values():
            disk = bytearray(image.durable)
            for kind, a, b in image.pending:
                if not keep_everything and rng.random() < 0.5:
                    continue  # this write never reached the disk
                if kind == "truncate":
                    del disk[a:]
                    continue
                data = b
                if not keep_everything and rng.random() < 0.25:
                    data = data[:rng.randrange(0, len(data) + 1, SECTOR)]  # torn write
                _write(disk, a, data)
            image.current, image.durable, image.pending = disk, bytearray(disk), []
