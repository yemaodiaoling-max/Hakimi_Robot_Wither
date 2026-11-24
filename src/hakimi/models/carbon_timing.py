"""One measured QQ turn sets the persistent Carbon turn budget."""
from contextlib import contextmanager
import json
import math
import time


@contextmanager
def turn_budget(state, coordinator, claim):
    import fcntl
    result = state / 'carbon_turn_timing.json'
    if result.exists():
        yield True, json.loads(result.read_text())['timeout_seconds'], None
        return
    with (state / 'carbon_turn_timing.lock').open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not coordinator.renew_carbon_wait(claim['token'], None):
                    raise RuntimeError('vertex_turn_ownership_lost')
                time.sleep(3)
        if result.exists():
            seconds = json.loads(result.read_text())['timeout_seconds']
            fcntl.flock(lock, fcntl.LOCK_UN)
            yield True, seconds, None
            return

        def save(elapsed, trigger):
            temp = result.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(trigger_id=trigger, measured_seconds=round(elapsed, 3),
                timeout_seconds=math.ceil(elapsed * 1.3), at=time.time())))
            temp.replace(result)
        yield True, None, save


# Carbon can spend several minutes thinking before it streams the first part.
MIN_IDLE_SECONDS = 360
MAX_IDLE_SECONDS = 480


class CarbonProgress:
    """Only model content, accepted tool calls and receipts renew this window."""
    delivery = {'reply.send', 'reply.skip', 'voice.speak', 'voice.hum', 'math.send', 'files.send',
                'chat.export', 'diary.export', 'members.mention', 'image.draw'}

    def __init__(self, coordinator, claim, measured, clock=time.monotonic):
        self.coordinator = coordinator
        self.token = claim['token']
        self.clock = clock
        self.started = self.last = clock()
        self.idle = max(MIN_IDLE_SECONDS, min(MAX_IDLE_SECONDS, measured or 0))
        self.saved = None
        deadline = claim.get('deadline')
        self.hard_deadline = deadline if deadline is not None and deadline - coordinator.clock() <= self.idle else None
        self.lease_deadline = None
        self.expires_at = claim.get('expires_at')
        self.handoff = False
        self.rounds = self.calls = 0
        self.nudged = None
        self.closing = False
        self.touch()

    def remaining(self):
        left = self.idle - (self.clock() - self.last)
        if self.hard_deadline is not None:
            left = min(left, self.hard_deadline - self.coordinator.clock())
        if left <= 0:
            raise TimeoutError('carbon_no_progress')
        return left

    def touch(self):
        self.remaining()
        now = self.clock()
        self.idle = max(self.idle, min(MAX_IDLE_SECONDS, math.ceil((now - self.last) * 1.5)))
        self.last = now
        if self.saved is None or now - self.saved >= 5:
            end = self.coordinator.clock() + self.idle + 5
            if self.hard_deadline is not None:
                end = min(end, self.hard_deadline)
            if not self._renew(end):
                raise RuntimeError('vertex_turn_ownership_lost')
            self.saved = now
            self.lease_deadline = end
        return True

    def _renew(self, end):
        bound = (dict(hard_expires_at=self.expires_at)
                 if self.handoff and self.expires_at is not None else {})
        return self.coordinator.renew_carbon_wait(self.token, end, rolling=True, **bound)

    def freeze_for_handoff(self):
        now = self.coordinator.clock()
        end = self.lease_deadline
        if self.expires_at is not None:
            reserve = min(self.coordinator.fallback_timeout_seconds, max(0, (self.expires_at - now) / 2))
            end = min(end, self.expires_at - reserve)
        if end - now <= 5:
            return False
        self.handoff = True
        if not self._renew(end):
            return False
        self.hard_deadline = self.lease_deadline = end
        return True

    def prepare(self, names, contents):
        elapsed = self.clock() - self.started
        if self.nudged is None and (self.rounds >= 5 or self.calls >= 12 or elapsed >= MIN_IDLE_SECONDS):
            contents[-1]['parts'].append({'text': '请整理已有材料作答，仅在还差关键依据时再查。'})
            self.nudged = self.rounds
        elif self.nudged is not None and (self.rounds >= self.nudged + 2 or self.calls >= 18 or elapsed >= MAX_IDLE_SECONDS):
            if self.closing:
                # A failed delivery tool or a mention lookup can still finish as plain text.
                return set()
            self.closing = True
            contents[-1]['parts'].append({'text': '现在用已有结果完成回复，证据不足处可直接说明。'})
        return names & self.delivery if self.closing else names


def delivery_ack(state, trigger, status):
    """The official worker records terminal delivery only for the measured turn."""
    if not trigger:
        return
    from pathlib import Path
    watch = Path(state) / 'carbon_delivery_watch'
    try:
        if watch.read_text() != trigger:
            return
        receipt = watch.with_name('carbon_delivery_receipt')
        tmp = receipt.with_suffix('.tmp')
        tmp.write_text(json.dumps([trigger, status]))
        tmp.replace(receipt)
    except OSError:
        pass


@contextmanager
def watch_delivery(runtime, trigger):
    from hakimi.bridge.delivery_guard import DEFAULT_POLICY
    from pathlib import Path
    folder = Path(runtime) / 'state'
    watch = folder / 'carbon_delivery_watch'
    receipt = folder / 'carbon_delivery_receipt'
    receipt.unlink(missing_ok=True)
    watch.write_text(trigger)

    def delivered():
        until = time.monotonic() + DEFAULT_POLICY['max_queue_age_seconds']
        while time.monotonic() < until:
            if receipt.exists():
                row = json.loads(receipt.read_text())
                if row[0] == trigger:
                    return row[1] == 'delivered'
            time.sleep(1)
        return False

    try:
        yield delivered
    finally:
        watch.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
