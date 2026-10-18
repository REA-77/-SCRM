"""A bounded, single-worker queue for explicitly requested platform writes."""
import logging
import queue
import threading


class TaskQueue:
    def __init__(self, execute, capacity=100):
        self.execute = execute
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.active = set()
        self.worker = None

    def submit(self, task_id):
        with self.lock:
            if task_id in self.active:
                return 'duplicate'
            if self.queue.full():
                return 'full'
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, name='platform-writes', daemon=True)
                self.worker.start()
            self.active.add(task_id)
            self.queue.put_nowait(task_id)
            return 'queued'

    def _run(self):
        while True:
            task_id = self.queue.get()
            try:
                self.execute(task_id)
            except Exception:
                logging.getLogger(__name__).exception('Task %s execution failed', task_id)
            finally:
                with self.lock:
                    self.active.discard(task_id)
                self.queue.task_done()
