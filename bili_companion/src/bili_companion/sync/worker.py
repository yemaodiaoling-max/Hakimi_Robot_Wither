"""Small in-process periodic worker. No Codex chats, model calls, or services."""
import logging
import time
from typing import Protocol
from .compress import compress_batches
from .types import SourceBatch, number


class PacketTransport(Protocol):
    def send(self, packet: dict) -> bool:
        """True only after peer durable receive; timeouts may safely retry same ID."""
        ...


class SyncWorker:
    def __init__(self, store, source, transport, *, interval_seconds=None, clock=None, logger=None):
        self.store, self.source, self.transport = store, source, transport
        self.interval = interval_seconds if interval_seconds is not None else (1800 if store.endpoint == 'bilibili' else 21600)
        number(self.interval, minimum=1)
        self.clock = clock or store.clock
        self.log = logger or logging.getLogger('bili_companion.sync')

    def run_once(self, *, force=False):
        token = self.store.claim(force=force)
        if not token:
            return {'state': 'not_due', 'sent': 0}
        sent = 0
        try:
            pending = self.store.pending()
            if not pending:
                checkpoint = self.store.status()['checkpoint']
                batch = self.source(checkpoint)
                if type(batch) is not SourceBatch:
                    raise ValueError('source must return SourceBatch')
                batches = compress_batches(batch.items, endpoint=self.store.endpoint, now=self.clock())
                self.store.stage_batches(batches, expected_checkpoint=checkpoint, checkpoint=batch.checkpoint, lease_token=token)
                if getattr(self.source, 'renew_active', True):
                    self.store.renew(lease_token=token)
                pending = self.store.pending()
            for packet in pending:
                self.store.attempted(packet['packet_id'], lease_token=token)
                if self.transport.send(packet) is not True:
                    raise OSError('peer did not durably acknowledge')
                self.store.acknowledge(packet['packet_id'], lease_token=token)
                sent += 1
            # A bounded pass does not silently postpone an unflushed backlog.
            success = not self.store.pending(limit=1)
            self.store.finish(success=success, interval_seconds=self.interval, lease_token=token)
            state = 'synced' if sent else 'empty'
            self.log.info('sync endpoint=%s state=%s packets=%d', self.store.endpoint, state, sent)
            return {'state': state, 'sent': sent}
        except Exception as exc:
            try:
                self.store.finish(success=False, interval_seconds=self.interval, lease_token=token)
            except ValueError:
                pass  # A replacement worker owns the lease; never clear its state.
            # No exception text: upstream errors may include raw text or secrets.
            self.log.warning('sync endpoint=%s state=retry error_type=%s', self.store.endpoint, type(exc).__name__)
            return {'state': 'retry', 'sent': sent, 'error_type': type(exc).__name__}

    def daemon(self, stop_event):
        while not stop_event.is_set():
            self.run_once()
            delay = max(.1, min(30, self.store.status()['next_due']-self.clock()))
            stop_event.wait(delay)
