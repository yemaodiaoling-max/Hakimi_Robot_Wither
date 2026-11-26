"""Local, bounded turn-taking model. No network, LLM, or tools are called here.

Mentions, batched participation and related follow-ups share one decision model.
Delays and participation pressure are continuous; transport safety is separate.
Small decaying statistics, hashed topic features and a short own-reply excerpt
survive restart; human messages stay in the existing inbox.
"""
import hashlib
import json
import math
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_SESSIONS = 128
MAX_STATE_BYTES = 2 * 1024 * 1024
SESSION = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")
QUESTION = re.compile(r"[?？]|怎么|为什么|为啥|如何|请教|求助|谁知道|能否|有没有|求解|吗|嘛|吧|么|捏|不[?？]|真的假的|真假|是不是|对不对|行不行|好不好|难道")
FOLLOWUP = re.compile(
    r"^(?:那(?:这样|这个|这|现在|怎么|为啥|为什么|能|要|还|不|是|就|你|她|他|它|我|谁|也|其实|到底|真|倒|可|有|没)|"
    r"这(?:个|样|么)|所以|也就是|意思是|可是|但是|不过|不对|等下|等等|然后|后来|还有)|"
    r"为什么|为啥|怎么会|没(?:看|听|太)?懂|能(?:再|不能)|再(?:说|讲)|展开|继续|接着|具体(?:呢|一点)|你(?:的意思|说|能|看|觉得|知道|理理)|"
    r"真(?:的|假|有)|确(?:实|实是)|还真|谁说|才不|我不信|你(?:别|又|真|还|倒|才|是|居然|敢)|难道|是不是|对不对|行不行|好不好|"
    r"怎么不|为何|啥时候|什么时候|在哪|哪个|哪位|啥意思|什么意思|啥内容|什么内容|发了什么|说了什么|在说什么|聊什么|"
    r"装死|好假|太假|好怪|笑死|太强|好厉害|真的假的|还真是|真就|怎么就|这就|谁问|问你"
)
TOPIC_CHANGE = re.compile(r"^(?:换个话题|聊点别的|说点别的|另外问|顺便问|话说|对了|今晚|明天|明晚|那?晚上吃)")


def enabled(cfg):
    return cfg.get("conversation_scheduler", {}).get("enabled") is True


def plain_text(text):
    text = re.sub(r"\[(?:群友发图|表情包图片|QQ表情|表情)[^\]]*\]", " ", str(text)[:6000])
    text = re.sub(r"\[CQ:[^\]]*\]|https?://\S+|[A-Za-z]:[/\\]\S+", " ", text).lower()
    text = re.sub(r"\[引用.*?\]\s*", " ", text, flags=re.S)
    return text.strip()


def features(text):
    """Cheap lexical relevance, deliberately not a semantic/knowledge claim."""
    text = plain_text(text)
    words = re.findall(r"[a-z][a-z0-9_]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", text)
    units = set()
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            units.update(word[i:i+2] for i in range(len(word)-1)
                         if word[i:i+2] not in {"哈哈", "哈基", "基米", "喵喵", "这个", "那个", "就是", "然后", "可以", "什么", "为什", "怎么", "这样", "那样", "所以", "因为", "真的", "觉得", "感觉", "不是", "还是", "但是", "意思", "一下", "的话", "知道", "应该", "有没", "没有"})
        else:
            units.add(word)
    if re.fullmatch(r"\s*(?:好的|好哒|收到|谢谢|谢谢你|嗯嗯|哈哈|喵|ok|lol)[！!。 .]*", text):
        return set()
    return {hashlib.sha256(unit.encode()).hexdigest()[:16] for unit in sorted(units)[:96]}


def similarity(a, b):
    if not a or not b:
        return 0.0
    shared = len(a & b)
    # A focused short follow-up must not be penalized just because the answer
    # being followed up was long. Generic turn words are removed above.
    return .55*shared/math.sqrt(len(a)*len(b)) + .45*shared/max(3, min(len(a), len(b)))


def actor(user_id):
    return hashlib.sha256(("actor:"+str(user_id)).encode()).hexdigest()[:16]


def decay(value, elapsed, half_life):
    return value * math.exp(-math.log(2) * max(0, elapsed) / half_life)


@dataclass
class GroupState:
    # Runtime messages never go into the scheduler checkpoint.
    pending: deque = field(default_factory=lambda: deque(maxlen=64))
    human_times: deque = field(default_factory=lambda: deque(maxlen=160))
    pending_since: float = 0.0
    at: float = 0.0
    calls: float = 0.0
    replies: float = 0.0
    skips: float = 0.0
    last_reply: float = 0.0
    last_offer: float = 0.0
    anchor: set = field(default_factory=set)
    source: set = field(default_factory=set)
    memory_source: set = field(default_factory=set)
    offered: set = field(default_factory=set)
    own_ids: deque = field(default_factory=lambda: deque(maxlen=12))
    reply_users: set = field(default_factory=set)
    # Replies within a warm dialogue and the post-mention listening window.
    cooldown_until: float = 0
    turns: float = 0
    turn_at: float = 0
    listen_round: float = 0
    listen_started: float = 0
    listen_due: float = 0
    listen_cutoff: float = 0
    listen_next_check: float = 0
    listen_review_started: bool = False
    reply_lane: str = ""
    listen_reply: str = ""
    listen_source_ids: list = field(default_factory=list)
    listen_trigger_id: str = ""
    listen_delivery_pending: bool = False
    window_messages: list = field(default_factory=list)
    window_collecting: bool = False
    context_high_water: float = 0.0
    first_reply_author: str = ""


@dataclass(frozen=True)
class Decision:
    mode: str
    ready: bool
    score: float
    wait: float
    relevance: float = 0.0
    activity: float = 0.0
    pressure: float = 0.0
    basis: str = "none"

    def metadata(self):
        return {"mode": self.mode, "score": round(self.score, 3),
                "relevance": round(self.relevance, 3), "activity": round(self.activity, 3),
                "pressure": round(self.pressure, 3), "basis": self.basis}


class ConversationScheduler:
    def __init__(self):
        self.groups = OrderedDict()
        self.global_calls = 0.0
        self.global_at = 0.0
        self.last_outcome = ""
        self.completed_outcomes = deque(maxlen=128)
        self.offer_sources = OrderedDict()
        self.path = None
        self.healthy = True
        self.audit_seen = {}
        self.on_window_closed = None

    def group(self, session):
        if not SESSION.fullmatch(session):
            raise ValueError("invalid scheduler session")
        if session not in self.groups:
            if len(self.groups) >= MAX_SESSIONS:
                # Do not evict an accumulating group just because another is noisy.
                cold = next((key for key, state in self.groups.items() if not state.pending), None)
                if cold is None:
                    raise ValueError("scheduler session capacity")
                self.groups.pop(cold)
            self.groups[session] = GroupState()
        self.groups.move_to_end(session)
        return self.groups[session]

    def observe(self, session, text, user_id, message_id, at, *, mentioned=False, image=False, reply_to=None, addressed_other=False, memory_nodes=()):
        state = self.group(session)
        if any(row[2] == str(message_id) for row in state.pending):
            return
        if not state.pending:
            state.pending_since = at
        plain = plain_text(text)
        is_topic_change = bool(TOPIC_CHANGE.search(plain))
        is_pure_ack = bool(re.fullmatch(r"\s*(?:好的|好哒|收到|谢谢|谢谢你|嗯+|哈哈+|喵|ok|lol)[！!。 .]*", plain))
        followup = (bool(FOLLOWUP.search(plain)) or bool(re.search(r"(?:吗|嘛|吧|么|捏)[?？!！]*\Z", plain))) and not is_topic_change
        state.pending.append((at, str(user_id), str(message_id), features(text),
                              bool(QUESTION.search(plain)), bool(mentioned), bool(image), str(reply_to or ""),
                              bool(addressed_other), bool(followup), bool(is_topic_change), bool(is_pure_ack), tuple(memory_nodes)[:6]))
        state.human_times.append((at, str(user_id)))

    def _pressures(self, state, now):
        return (decay(state.calls, now-state.at, 300), decay(state.replies, now-state.at, 240),
                decay(state.skips, now-state.at, 600), decay(self.global_calls, now-self.global_at, 600))

    def _relation(self, row, state):
        if len(row) > 8 and row[8]:
            return 0.0, "addressed_other"
        if row[7] in state.own_ids:
            return .98, "explicit_reference"
        if not state.last_reply:
            return 0.0, "none"
        gap = max(0, row[0]-state.last_reply)
        lexical = max(similarity(row[3], state.anchor), similarity(row[3], state.source)) * math.exp(-gap/300)
        contextual = 0.0
        basis = "lexical" if lexical else "none"
        # Context cues estimate an omitted subject. They need an actual prior
        # sent reply and conversational proximity; messages during generation
        # cannot implicitly be replying to an answer they have not yet seen.
        if row[0] >= state.last_reply:
            audience = 1.0 if actor(row[1]) in state.reply_users else .45
            people = {uid for at, uid in state.human_times if state.last_reply <= at <= row[0]}
            continuity = 1/(1+max(0, len(people)-1)/3)
            cue = len(row) > 9 and row[9]
            is_topic_change = len(row) > 10 and row[10]
            if row[6] and not row[3] and audience == 1:
                contextual = .38*continuity*math.exp(-gap/80)
                basis = "image_followup" if contextual > lexical else basis
            elif audience == 1.0 and not is_topic_change:
                # Same user speaking again in warm window without changing topic:
                # Intrinsically valid human follow-up turn, no whitelist needed!
                base_strength = 0.78 if cue else 0.65
                contextual = base_strength * continuity * math.exp(-gap / (150 if cue else 90))
                basis = "contextual_followup" if contextual > lexical else basis
            elif cue:
                contextual = .78*audience*continuity*math.exp(-gap/180)
                basis = "contextual_followup" if contextual > lexical else basis
        memory=0.0
        if len(row)>12 and not row[10] and state.memory_source:
            shared=set(row[12]) & state.memory_source
            documents=sum(node.startswith('d') for node in shared)
            entities=sum(node.startswith('e') for node in shared)
            memory=(.58 if documents else .32 if entities>=2 else .12 if entities else 0)*math.exp(-gap/300)
        if memory>max(lexical,contextual):
            return memory,'memory_bridge'
        return max(lexical, contextual), basis

    def decide(self, session, cfg, now):
        state = self.group(session)
        profile = cfg.get("bot_profile", {})
        kind, gid = session.split(":", 1)
        if ((kind == "group" and (gid in {str(v) for v in profile.get("ignored_groups", [])}
                or (profile.get("active_groups") and gid not in {str(v) for v in profile["active_groups"]})))
                or (kind == "private" and not profile.get("allow_private_chat", True))):
            return Decision("silent", False, 0, 30)
        from hakimi.chat.bot_registry import labels
        bots=labels(self.path.parent.parent) if self.path else {}
        cooled={uid for uid,row in bots.items() if row['status']=='confirmed' and row['cooldown']>now}
        pending = [row for row in state.pending if 0 <= now-row[0] <= 600 and str(row[1]) not in cooled]
        if kind == "private":
            if pending:
                return Decision("mention", True, 5, 0, basis="explicit_mention")
            return Decision("silent", False, 0, 30)
        if any(row[5] for row in pending) and cfg.get("lurker_schedule", {}).get("instant_wake_on_at_or_private", True):
            delay = self._setting(
                cfg.get("conversation_scheduler", {}), "mention_buffer_seconds", 1, 0, 1)
            first = min(row[0] for row in pending if row[5])
            wait = max(0, first+delay-now)
            return Decision("mention", not wait, 5, wait,
                            basis="collecting_mention" if wait else "explicit_mention")
        if (not self.healthy or not cfg.get("lurker_schedule", {}).get("enable_random_window_peek", True)
                or not cfg.get("participation_policy", {}).get("enabled", True)):
            return Decision("silent", False, 0, 30)
        if state.cooldown_until > now:
            return Decision("silent", False, 0, state.cooldown_until-now, basis="dialogue_cooldown")
        if state.listen_round:
            if state.listen_delivery_pending or not state.listen_due:
                return Decision("listening", False, 0, 5, basis="awaiting_reply_delivery")
            # Eligibility for one NEW batch, not permission to call the reply model.
            # Once frozen, new arrivals cannot slide the cutoff or restart DSH.
            if state.listen_cutoff:
                return Decision("listening", True, 0, 0, basis="awaiting_refinement")
            if now >= state.listen_due and state.listen_next_check > state.listen_due:
                self.consume(session, through=state.listen_due)
                self.stop_listening(session)
                return Decision("silent", False, 0, 30, basis="window_finished")
            arrivals = [(row[0], not row[11] and bool(row[3] or row[4] or row[6] or row[9]))
                        for row in pending if state.listen_started < row[0] <= state.listen_due]
            for row in state.window_messages:
                at = float(row.get('received_at', 0))
                if (str(row.get('user_id')) in cooled or row.get('user_id') == 'SELF'
                        or not state.listen_started < at <= state.listen_due):continue
                text = plain_text(row.get('text', ''))
                ack = bool(re.fullmatch(r"\s*(?:好的|好哒|收到|谢谢|谢谢你|嗯+|哈哈+|喵|ok|lol)[！!。 .]*", text))
                arrivals.append((at, bool(row.get('image_paths') or text and not ack)))
            if not any(meaningful for _, meaningful in arrivals):
                if now >= state.listen_due:
                    self.consume(session, through=state.listen_due)
                    self.stop_listening(session)
                    return Decision("silent", False, 0, 30, basis="empty_window")
                return Decision("listening", False, 0, state.listen_due-now, basis="collecting_window")
            latest = max(at for at, _ in arrivals)
            # Quiet allows an early first batch; continuous traffic gets a 15s check.
            # Later batches need fresh input and are spaced from the last DSH start.
            collect_until = min(state.listen_due, state.listen_next_check or
                                min(state.last_reply+15, max(state.last_reply+6, latest+3)))
            if now < collect_until:
                return Decision("listening", False, 0, collect_until-now, basis="collecting_window")
            state.listen_cutoff = min(now, state.listen_due)
            self.save()
            return Decision("listening", True, 0, 0, basis="ready_for_refinement")
        return self._score_rows(session, cfg, now, pending)

    def score_window_messages(self, session, cfg, now, messages, *, reviewed_message_ids=()):
        """Recheck only model-selected window inputs; background supplies no value."""
        state = self.group(session)
        if state.cooldown_until > now:
            return Decision("listening", False, 0, state.cooldown_until-now, basis="dialogue_cooldown")
        converter = ConversationScheduler()
        for item in messages:
            at = float(item.get("received_at", 0))
            if (item.get("user_id") == "SELF" or not state.listen_started < at <= (state.listen_cutoff or state.listen_due)):
                continue
            targets = item.get("direct_targets", ())
            if not isinstance(targets, (list, tuple, set)):
                targets = ()
            converter.observe(session, item.get("text", ""), item.get("user_id", ""),
                              item.get("message_id", ""), at,
                              image=bool(item.get("image_paths") or item.get("image")),
                              reply_to=item.get("reply_to_message_id", item.get("reply_to")),
                              addressed_other=bool(item.get("addressed_other") or
                                  (targets and
                                   str(cfg.get("bot_profile", {}).get("self_qq", "")) not in
                                   {str(value) for value in targets})))
        rows = list(converter.group(session).pending)
        reviewed_ids = set(reviewed_message_ids)
        decision = self._score_rows(session, cfg, now, rows, listening=True,
                                    reviewed=any(str(row[2]) in reviewed_ids for row in rows))
        if decision.mode == 'silent':
            decision = replace(decision, basis='review_outside_window' if messages and not rows else 'review_not_actionable')
        return decision

    def _score_rows(self, session, cfg, now, pending, *, listening=False, reviewed=False):
        state = self.group(session)
        profile = cfg.get("bot_profile", {})
        kind, gid = session.split(":", 1)
        if not pending:
            return Decision("silent", False, 0, 30)

        # The last few *new* messages determine whether the conversation is still
        # related; old relevant messages cannot justify joining a changed topic.
        actionable = [row for row in pending if row[3] or row[6] or row[7] in state.own_ids
                      or (len(row)>9 and row[9])
                      or (state.last_reply and actor(row[1]) in state.reply_users and row[0] >= state.last_reply
                          and not (len(row)>11 and row[11]) and not (len(row)>10 and row[10]))]
        if not actionable:
            return Decision("silent", False, 0, 30)
        # Plain acknowledgements do not push a still-pending question out of
        # scope. A substantive new topic still overrides an older related one.
        tail = actionable[-12:] if listening else actionable[-4:]
        decay_per_message = .8 if listening else .2
        relations = [(value*decay_per_message**index, basis) for index, row in enumerate(reversed(tail))
                     for value, basis in [self._relation(row, state)]]
        relevant, basis = max(relations, default=(0.0, "none"))
        if listening and reviewed:
            relevant = .65 + .35*relevant  # DSH already established the conversational connection.
        reference = tail[-1][7] in state.own_ids
        topic = set().union(*(row[3] for row in tail))
        meaningful = len(actionable)
        if not meaningful and not reference:
            return Decision("silent", False, 0, 30)
        recent = [(at, uid) for at, uid in state.human_times if 0 <= now-at <= 180]
        flow = sum(math.exp(-(now-at)/60) for at, _ in recent)
        activity = flow / (flow + 5)
        question = float(any(row[4] and not (len(row)>8 and row[8]) for row in tail))
        repetition = similarity(topic, state.offered)
        novelty = 1 - repetition
        calls, replies, skips, global_calls = self._pressures(state, now)
        settings = cfg.get("conversation_scheduler", {})
        turns = self.dialogue_turns(state, now, settings)
        continuing = listening or relevant >= .22
        if continuing:
            # Her own recent replies are what *created* this conversation, so they
            # must not be counted as "too chatty" the way unsolicited batches are.
            # Back-off for a dialogue comes from (a) skips, (b) a per-dialogue turn
            # budget, not from raw reply count. Listening windows stay fixed at 45s.
            pressure = .65*skips + .12*global_calls + .22*max(0, turns-1)
            if listening and reviewed:
                pressure *= .35
            if turns >= self.max_turns(settings):
                return Decision("continuation", False, 0, 30, relevant, 0.0, pressure, basis)
        else:
            # Busy groups must not exhaust a quiet group's first chance to join.
            shared = .23*global_calls/(1+global_calls/4)
            shared *= .25+.75*min(1, calls+replies)
            pressure = (.40*calls + .75*replies + .65*skips + shared) * (1-.85*relevant)
        study = gid in {str(v) for v in cfg.get("participation_policy", {}).get("study_groups", [])}
        low = gid in {str(v) for v in profile.get("low_frequency_groups", [])} or gid not in {str(v) for v in profile.get("random_peek_groups", [])}
        sensitivity = settings.get("busy_group_sensitivity", {}).get(gid, 1.0)
        sensitivity = float(sensitivity) if type(sensitivity) in (float, int) and math.isfinite(sensitivity) else 1.0
        sensitivity = max(.5, min(3, sensitivity))
        other_share = sum(bool(len(row)>8 and row[8]) for row in tail)/len(tail)
        intrusion = .35 + activity*sensitivity*(1-.75*relevant) + pressure + (.24 if low and not study else 0) + .9*other_share
        opportunity = .55 + .6*question + .5*novelty + .28*math.log1p(meaningful) + 1.35*relevant
        social = {}
        if self.path is not None:
            from hakimi.social.social_behavior import behavior_for
            social = behavior_for(self.path.parent.parent, tail[-1][1], session, now=now, config=cfg)
            opportunity *= social['initiative_multiplier']
            closeness = social.get('closeness')
            if type(closeness) in (int, float) and math.isfinite(closeness):
                intrusion *= 1 + .8*(1-max(0, min(100, closeness))/100)**2
        score = opportunity-intrusion
        if self.path is not None:
            from hakimi.social.social_behavior import group_initiative_score
            score = group_initiative_score(session, score, social)
        # More busy human conversation lengthens unsolicited batching; clear
        # continuation accelerates it. Repeated calls/replies slow it smoothly.
        target = (45 + 125*activity*sensitivity + 75*pressure) / (1 + 4*relevant + .65*question + .35*math.log1p(meaningful))
        if study:
            target *= .85
        if continuing:
            # Warm, related follow-ups should reach the model before ordinary
            # chatter buries them. Scores/turn budgets still decide eligibility.
            warmth = math.exp(-max(0, now-state.last_reply)/180)
            target *= (1 + .5*max(0, turns-1)) / (1 + 6*relevant*warmth)
        target = max(8, min(360, target))  # bounded freshness/transport envelope
        age = now-state.pending_since
        wait = max(0, target-age)
        # Insufficient value does not become permission simply by waiting.
        if listening:
            wait = 0  # The fixed collection window already supplied the wait.
        ready = score >= .45 and wait <= 0
        if score < .45:
            wait = 30.0
        return Decision("listening" if listening else "continuation" if relevant >= .22 else "batch", ready, score, wait,
                        relevant, activity, pressure, basis)

    @staticmethod
    def _setting(settings, name, default, low, high):
        value = settings.get(name, default)
        if type(value) not in (int, float) or not math.isfinite(value):
            return default
        return max(low, min(high, value))

    def dialogue_turns(self, state, now, settings=None):
        """How many of her replies belong to the dialogue that is still 'warm'."""
        window = self._setting(settings or {}, "dialogue_window_seconds", 240, 30, 900)
        return int(state.turns) if state.listen_round or (state.turn_at and 0 <= now-state.turn_at <= window) else 0

    def max_turns(self, settings=None):
        return int(self._setting(settings or {}, "continuation_max_turns", 4, 1, 12))

    def choose(self, cfg, now, *, exclude=()):
        candidates = []
        collecting = False
        for session in list(self.groups):
            if session in exclude:
                continue
            decision = self.decide(session, cfg, now)
            collecting |= decision.mode == "mention" and not decision.ready
            if decision.ready:
                state = self.groups[session]
                fairness = min(.4, max(0, now-state.last_offer)/9000) if state.last_offer else .4
                priority = 2 if decision.mode == "mention" else 1 if decision.mode == "listening" else 0
                candidates.append((priority, decision.score+fairness, -state.last_offer, session, decision))
        if collecting:
            candidates = [row for row in candidates if row[0] == 2]
        return max(candidates, default=(None, None, None, None, None))[-2:]

    def audit_decision(self, session, decision, now, phase="receipt", *, selection_counts=None):
        """Bounded diagnostic metadata, never chat bodies, user IDs or tokens."""
        if self.path is None:
            return
        state = self.group(session)
        if not state.pending:
            return
        message_id = state.pending[-1][2][:30]
        signature = (message_id, decision.mode, decision.ready, decision.basis, phase, int(decision.wait/15))
        previous = self.audit_seen.get(session, (None, 0))
        if (signature == previous[0] and now-previous[1] < 60
                or now-previous[1] < 10 and phase != "refined_selection" and not (decision.ready and phase == "selection")):
            return
        self.audit_seen[session] = (signature, now)
        gate = "busy" if phase == "busy" else ("ready" if decision.ready else (
            "no_actionable_input" if decision.mode == "silent" else "waiting_window" if decision.basis == "collecting_window" else "waiting_batch" if decision.score >= .45 else "insufficient_value"))
        row = {"version": 1, "at": now, "session_key": session, "last_message_id": message_id,
               **decision.metadata(), "phase": phase, "gate": gate, "wait_seconds": round(decision.wait, 2)}
        if selection_counts is not None:
            row['selection_counts'] = selection_counts
        try:
            path = self.path.parent / "conversation_decisions.jsonl"
            if path.is_symlink() or path.resolve() != path.absolute():
                return
            if path.exists() and path.stat().st_size > 512*1024:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size-256*1024))
                    kept = stream.read(256*1024).partition(b"\n")[2]
                temporary = path.with_suffix(".rotate.tmp")
                if temporary.is_symlink():
                    return
                temporary.write_bytes(b"\n".join(kept.splitlines()[-1000:])+b"\n")
                from hakimi.core.runtime_io import atomic_replace
                atomic_replace(temporary,path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=True, separators=(",", ":"))+"\n")
        except (OSError, ValueError):
            pass  # Diagnostics cannot block or retry a conversation turn.

    def consume(self, session, through=None):
        state = self.group(session)
        if through is None:
            state.pending.clear()
        else:
            state.pending = deque((row for row in state.pending if row[0] > through), maxlen=64)
        state.pending_since = state.pending[0][0] if state.pending else 0
        state.window_messages = [] if through is None else [row for row in state.window_messages if row.get("received_at", 0) > through]

    def offered_turn(self, session, now, trigger_id="", messages=None, wake_mode="", memory_nodes=(), reply_author="", conversation_lane=""):
        state = self.group(session)
        calls, replies, skips, global_calls = self._pressures(state, now)
        state.calls, state.replies, state.skips, state.at = calls+1, replies, skips, now
        state.offered = set(sorted(set().union(*(row[3] for row in state.pending)))[:128])
        if trigger_id:
            if messages is None:
                topic = set().union(*(row[3] for row in list(state.pending)[-4:]))
                users = {actor(row[1]) for row in list(state.pending)[-4:]}
            else:
                human = [row for row in messages if row.get("user_id") != "SELF"][-4:]
                topic = set().union(*(features(str(row.get("text", ""))[:1500]) for row in human))
                users = {actor(row.get("user_id", "")) for row in human}
            self.offer_sources[trigger_id] = {"session": session, "features": sorted(topic)[:128], "participants": sorted(users), "wake_mode": wake_mode, "conversation_lane": conversation_lane}
            if messages is not None:
                self.offer_sources[trigger_id]['context_message_ids'] = [str(row['message_id']) for row in messages
                    if row.get('user_id') != 'SELF' and row.get('message_id')][-12:]
                self.offer_sources[trigger_id]['message_ids']=[str(row.get('message_id','')) for row in human][-4:]
                self.offer_sources[trigger_id]['message_authors'] = {str(row.get('message_id', '')): actor(row.get('user_id', ''))
                    for row in messages if row.get('user_id') != 'SELF'}
            if memory_nodes:
                self.offer_sources[trigger_id]['memory_nodes']=list(memory_nodes)[:6]
            while len(self.offer_sources) > 32:
                self.offer_sources.popitem(last=False)
        if messages:
            state.context_high_water = max([state.context_high_water, *[float(row.get("received_at", 0)) for row in messages if row.get("user_id") != "SELF"]])
        if wake_mode != "被@后监听窗口" or not state.first_reply_author:
            state.first_reply_author = actor(reply_author) if reply_author else ""
        state.last_offer = now
        self.global_calls, self.global_at = global_calls+1, now
        if wake_mode.startswith("即时唤醒"):
            self.stop_listening(session, save=False)
        elif wake_mode == "被@后监听窗口":
            state.listen_due = state.listen_cutoff = state.listen_next_check = 0
            state.listen_review_started = False
            state.listen_delivery_pending = False
        state.window_collecting = True
        self.save()

    def reject_window_batch(self, session, through, now):
        """A rejected batch is consumed once; fresh input may use the next slot."""
        state = self.group(session)
        self.consume(session, through=through)
        state.context_high_water = max(state.context_high_water, through)
        state.listen_started = max(state.listen_started, through)
        state.listen_cutoff = 0
        state.listen_review_started = False
        if now >= state.listen_due:
            self.consume(session, through=state.listen_due)
            self.stop_listening(session, save=False)
        self.save()

    def stop_listening(self, session, save=True):
        state = self.group(session)
        if self.on_window_closed is not None and (state.listen_round or state.listen_reply or state.listen_delivery_pending):
            self.on_window_closed(session, state.listen_delivery_pending)
        state.listen_round = state.listen_started = state.listen_due = state.listen_cutoff = state.listen_next_check = 0
        state.listen_review_started = False
        state.listen_reply = ""
        state.listen_source_ids = []
        state.listen_trigger_id = ""
        state.listen_delivery_pending = False
        state.window_collecting = False
        if save:self.save()

    def delivery_finished(self, session, now):
        state = self.group(session)
        if state.listen_delivery_pending:
            state.listen_delivery_pending = False
            if state.listen_round:
                state.last_reply = now
                state.listen_due = now+45
        elif state.listen_round and not state.listen_due:
            self.stop_listening(session, save=False)
        self.save()

    def observe_reply(self, session, now):
        """Extend the cutoff on each confirmed QQ bubble, including the last."""
        state = self.group(session)
        if state.listen_round:
            state.last_reply = now
            state.listen_due = now+45
            self.save()

    def listening_context(self, session, now):
        state = self.group(session)
        cutoff = state.listen_cutoff or state.listen_due or now
        return {"round": int(state.listen_round), "wait_seconds": round(max(0, cutoff-state.last_reply), 2),
                "started_at": state.listen_started, "collected_until": cutoff,
                "new_message_ids": [row[2] for row in state.pending if state.listen_started < row[0] <= cutoff],
                "previous_reply_excerpt": state.listen_reply,
                "casual_length_scale": round(max(.45, 1/(1+.25*state.listen_round)), 2)}

    def sent_reply(self, session, text, now, message_id=None, trigger_id="", cfg=None):
        state = self.group(session)
        calls, replies, skips, _ = self._pressures(state, now)
        state.calls, state.replies, state.skips, state.at = calls, replies+1, skips, now
        window = self._setting({}, "dialogue_window_seconds", 240, 30, 900)
        state.turns = self.dialogue_turns(state, now, (cfg or {}).get("conversation_scheduler", {}))+1
        state.turn_at = now
        state.last_reply, state.anchor = now, features(text)
        source = self.offer_sources.pop(trigger_id, {})
        state.source = set(source.get("features", [])) if source.get("session") == session else set()
        state.memory_source = set(source.get('memory_nodes',[])) if source.get('session')==session else set()
        state.reply_users = set(source.get("participants", [])) if source.get("session") == session else set()
        mode = source.get("wake_mode", "")
        state.reply_lane = source.get("conversation_lane", "")
        settings = (cfg or {}).get("conversation_scheduler", {})
        if session.startswith("group:") and state.turns >= self.max_turns(settings) and state.cooldown_until <= now:
            state.cooldown_until = now + self._setting(settings, "continuation_cooldown_seconds", 240, 30, 3600)
        if session.startswith("group:") and state.cooldown_until <= now:
            state.listen_round = state.listen_round+1 if mode == "被@后监听窗口" else 1
            state.listen_started, state.listen_due = state.context_high_water, 0
            state.listen_cutoff = state.listen_next_check = 0
            state.listen_review_started = False
            state.listen_delivery_pending = True
            state.window_collecting = True
            state.listen_reply = text[:600]
            state.listen_source_ids = list(source.get("context_message_ids", source.get("message_ids", [])))
            state.listen_trigger_id = trigger_id
        else:
            self.stop_listening(session, save=False)
        if message_id is not None:
            state.own_ids.append(str(message_id))
        self.save()

    def outcome(self, snapshot, now):
        trigger = snapshot.get("trigger_id", "")
        if snapshot.get("status") != "completed" or not trigger or trigger == self.last_outcome or trigger in self.completed_outcomes:
            return
        self.last_outcome = trigger
        self.completed_outcomes.append(trigger)
        session = snapshot.get("payload", {}).get("session_key", "")
        source = self.offer_sources.get(trigger, {})
        if source.get("wake_mode") == "被@后监听窗口" and snapshot.get("outcome") != "replied":
            self.stop_listening(session, save=False)
            self.offer_sources.pop(trigger, None)
        if session in self.groups and snapshot.get("outcome") == "no_reply":
            state = self.group(session)
            state.window_collecting = False
            state.window_messages.clear()
            calls, replies, skips, _ = self._pressures(state, now)
            state.calls, state.replies, state.skips, state.at = calls, replies, skips+1, now
        self.save()

    def save(self):
        if self.path is None or not self.healthy:
            return
        data = {"version": 1, "global_calls": self.global_calls, "global_at": self.global_at,
                "last_outcome": self.last_outcome, "completed_outcomes": list(self.completed_outcomes), "offer_sources": self.offer_sources, "groups": {key: {
                    **{name: getattr(state, name) for name in ("at", "calls", "replies", "skips", "last_reply", "last_offer", "turns", "turn_at", "listen_round", "listen_started", "listen_due", "context_high_water", "cooldown_until", "listen_cutoff", "listen_next_check")},
                    **{name: sorted(getattr(state, name)) for name in ("anchor", "source", "offered")},
                    "own_ids": list(state.own_ids), "reply_users": sorted(state.reply_users),
                    "listen_review_started": state.listen_review_started, "reply_lane": state.reply_lane,
                    "listen_source_ids": state.listen_source_ids, "listen_trigger_id": state.listen_trigger_id,
                    "first_reply_author": state.first_reply_author, "listen_reply": state.listen_reply,"memory_source":sorted(state.memory_source)} for key, state in self.groups.items()}}
        try:
            if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
                raise ValueError("redirected scheduler checkpoint")
            raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("scheduler checkpoint size")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            if temp.is_symlink():
                raise ValueError("redirected temporary checkpoint")
            temp.write_text(raw, encoding="utf-8")
            from hakimi.core.runtime_io import atomic_replace
            atomic_replace(temp,self.path)
        except (OSError, ValueError):
            self.healthy = False

    def restore(self, base, now):
        self.path = Path(base) / "state" / "conversation_scheduler.json"
        try:
            if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
                raise ValueError("redirected scheduler checkpoint")
            if not self.path.exists():
                return
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_STATE_BYTES+1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("scheduler checkpoint size")
            data = json.loads(raw)
            if data.get("version") != 1 or not isinstance(data.get("groups"), dict) or len(data["groups"]) > MAX_SESSIONS:
                raise ValueError("invalid scheduler checkpoint")
            def number(value):
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
                    raise ValueError("invalid scheduler number")
                return float(value)
            global_at = number(data["global_at"])
            if global_at > now:
                raise ValueError("scheduler clock rollback")
            groups = OrderedDict()
            for session, row in data["groups"].items():
                state = GroupState()
                state.memory_source={node for node in row.get('memory_source',[])[:6]
                                     if isinstance(node,str) and re.fullmatch('[de][0-9a-f]{15}',node)}
                if not SESSION.fullmatch(session):
                    raise ValueError("invalid scheduler session")
                for name in ("at", "calls", "replies", "skips", "last_reply", "last_offer"):
                    setattr(state, name, number(row[name]))
                for name in ("turns", "turn_at", "listen_round", "listen_started", "listen_due", "context_high_water", "cooldown_until", "listen_cutoff", "listen_next_check"):
                    setattr(state, name, number(row.get(name, 0)))
                # A process restart cannot recover an in-flight DSH result; do not
                # charge another refinement for that same window.
                if row.get('listen_review_started'):
                    state.listen_round = state.listen_started = state.listen_due = state.listen_cutoff = state.listen_next_check = 0
                state.first_reply_author = str(row.get("first_reply_author", ""))
                state.window_collecting = bool(state.listen_round)
                state.listen_source_ids = [str(mid) for mid in row.get("listen_source_ids", [])[:12] if isinstance(mid, (str, int))]
                state.listen_trigger_id = str(row.get("listen_trigger_id", ""))[:128]
                state.reply_lane = str(row.get("reply_lane", ""))
                if state.reply_lane not in ("", "at1", "at2", "at3"):raise ValueError("invalid reply lane")
                state.listen_reply = row.get("listen_reply", "")
                if not isinstance(state.listen_reply, str) or len(state.listen_reply) > 600:
                    raise ValueError("invalid listening reply excerpt")
                if state.listen_started and now-state.listen_started > 600:
                    state.listen_round = state.listen_started = state.listen_due = 0
                if max(state.at, state.last_reply, state.last_offer) > now:
                    raise ValueError("scheduler clock rollback")
                for name in ("anchor", "source", "offered"):
                    tokens = row[name]
                    if not isinstance(tokens, list) or len(tokens) > 768 or any(not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{16}", token) for token in tokens):
                        raise ValueError("invalid topic features")
                    setattr(state, name, set(tokens))
                ids = row["own_ids"]
                if not isinstance(ids, list) or len(ids) > 12 or any(not isinstance(mid, str) or not re.fullmatch(r"-?[0-9]{1,20}", mid) for mid in ids):
                    raise ValueError("invalid own message identifiers")
                state.own_ids = deque(ids, maxlen=12)
                users = row.get("reply_users", [])
                if not isinstance(users, list) or len(users)>4 or any(not isinstance(uid,str) or not re.fullmatch(r"[0-9a-f]{16}", uid) for uid in users):
                    raise ValueError("invalid reply audience")
                state.reply_users = set(users)
                groups[session] = state
            self.groups = groups
            self.global_calls, self.global_at = number(data["global_calls"]), global_at
            self.last_outcome = str(data.get("last_outcome", ""))[:100]
            self.completed_outcomes = deque(data.get("completed_outcomes", [])[-128:], maxlen=128)
            sources = data.get("offer_sources", {})
            if not isinstance(sources, dict) or len(sources) > 32:
                raise ValueError("invalid offered topic sources")
            checked = OrderedDict()
            for trigger, row in sources.items():
                if (not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trigger)
                        or not isinstance(row, dict) or not SESSION.fullmatch(str(row.get("session", "")))
                        or not isinstance(row.get("features"), list) or len(row["features"]) > 128
                        or any(not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{16}", token) for token in row["features"])):
                    raise ValueError("invalid offered topic source")
                checked[trigger] = row
                users = row.get("participants", [])
                if not isinstance(users, list) or len(users)>4 or any(not isinstance(uid,str) or not re.fullmatch(r"[0-9a-f]{16}", uid) for uid in users):
                    raise ValueError("invalid offered audience")
            self.offer_sources = checked
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            self.healthy = False
