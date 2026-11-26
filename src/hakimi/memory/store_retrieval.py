"""MemoryStore retrieval: scope rules and hybrid lexical/dense/graph recall within a budget."""
import re
import sqlite3
import struct
import time
from collections import OrderedDict
from hakimi.memory.lifecycle_rules import expanded_terms
from hakimi.memory.store_base import CHANNEL_WEIGHT, RRF_K, _normalise_vector, index_text, vectorize
from hakimi.memory.text import stamp, terms


_DESKTOP_PRIVATE_LEAK_RE = re.compile(
    r"私聊|单聊|小窗|私信|微信聊|"
    r"(?:找|跟|和|与)[^，。|；！？\s]{1,12}(?:聊社科|聊哲学|私下聊|单独聊)|"
    r"抖落.*私聊" + __import__("hakimi.core.owner", fromlist=["x"]).phrase_alternation("privacy_phrases")
)

_BILI_GENERIC_TERMS = frozenset({
    "今天", "明天", "昨天", "今晚", "昨晚", "明早", "早上", "上午", "中午", "下午",
    "晚上", "夜里", "凌晨", "刚才", "现在", "最近", "时候", "几点", "时间", "什么",
    "怎么", "为什么", "哪儿", "哪里", "好多", "一下", "一点", "一些", "这个", "那个",
    "还是", "就是", "已经", "开始", "出来", "进去", "回来", "过去", "起来", "自己",
    "大家", "我们", "你们", "他们", "觉得", "感觉", "可能", "应该", "哈哈", "笑死",
    "我了", "了吗", "么样", "讲了", "了什", "吃啥", "干嘛",
})


class StoreRetrievalMixin:
    """Mixin of MemoryStore; see memory_store."""

    # ------------------------------------------------------------------ scope
    @staticmethod
    def scope(session, users, cross_groups=False, include_inactive=False, *, voice_scoped=False):
        state_filter = "r.state!='deleted'" if include_inactive else "r.state='active'"
        if voice_scoped:state_filter += " AND (r.channel!='voice' OR r.session=?)"
        extra_params=[session] if voice_scoped else []
        users = {str(u) for u in users if re.fullmatch(r'[1-9][0-9]{0,19}', str(u))}
        peer = re.fullmatch(r'private:([1-9][0-9]{0,19})', str(session))
        if peer:users.add(peer[1])
        users = sorted(users)[:16]
        marks = ','.join('?' for _ in users) or 'NULL'
        private_sessions = ['private:'+user for user in users]
        private_marks = ','.join('?' for _ in private_sessions) or 'NULL'
        linked_private = f'r.session IN ({private_marks})'
        bili_scope = "r.session='self:bilibili' AND r.shared=1 AND r.user_id='SELF'"
        if cross_groups:
            return (f"({state_filter} AND r.kind!='dialogue' AND (r.session=? OR r.session LIKE 'group:%' OR {linked_private} OR ({bili_scope})))", [*extra_params, session, *private_sessions])
        return (f"({state_filter} AND r.kind!='dialogue' AND (r.session=? OR (r.session LIKE 'group:%' AND r.user_id IN ({marks}) AND (r.kind='chat' OR r.kind='person' AND r.shared=1)) OR (r.kind='lore' AND r.shared=1 AND r.session LIKE 'group:%') OR {linked_private} OR ({bili_scope})))", [*extra_params, session, *users, *private_sessions])

    # -------------------------------------------------------------- retrieval
    def retrieve(self, session, messages, target, *, now=None, query=None, budget=600,
                 cross_groups=False, limit=4, explain=False, active_topics=None):
        self.sync()
        now = time.time() if now is None else now
        query = str(target.get("text", "")) if query is None else str(query)
        query_terms = expanded_terms(query, terms)
        for words in (("称呼", "名字", "昵称", "叫我", "我是谁"), ("喜欢", "喜好", "讨厌", "不喜欢"),
                      ("研究", "在学", "专业", "方向", "学什么"), ("印象", "记忆", "评价", "关系", "认识"),
                      ("B站", "视频", "直播", "看片", "UP主")):
            if any(word in query for word in words):
                query_terms.update(terms(" ".join(words)))

        # Multi-turn context Query enrichment: prevent short-sentence or referential retrieval degradation
        is_short_or_referential = len(query.strip()) < 18 or bool(
            re.search(r"然后|那|为什么|怎么|这个|那个|后来|接着|他|她|它|这件|那件|后来呢|咋样|怎么样|如何|还有呢|对不对|是不是", query)
        )
        if is_short_or_referential and messages:
            recent_texts = [str(m.get("text", "")) for m in messages[-3:] if m.get("text")]
            if active_topics and isinstance(active_topics, (list, tuple, set)):
                recent_texts.extend(str(t) for t in list(active_topics)[:4])
            ctx_combined = " ".join(recent_texts)
            if ctx_combined.strip():
                query_terms.update(expanded_terms(ctx_combined, terms))

        query_vector_sparse = dict(struct.iter_unpack('<Hf', vectorize(query_terms)))
        users = {str(row.get("user_id", "")) for row in messages}
        focus = str(target.get("user_id", ""))
        users.add(focus)
        scope, params = self.scope(session, users, cross_groups)
        live = " AND (r.expires=0 OR r.expires>?)"
        candidates = OrderedDict()

        # Query inference happens before the retrieval DB transaction.  WAL
        # readers would not block writers, but keeping inference outside every
        # connection makes the "no inference under a write lock" rule explicit.
        dense_vector = None
        dense_model = None
        dense_revision = None
        try:
            encoder = self.encoder_instance()
            # The service may have restarted with a different model revision.  Learn
            # it before choosing DB candidate rows so old vectors are never mixed in.
            model, revision = self._refresh_encoder_identity(encoder)
            with self.db() as probe:
                has_vectors = probe.execute(
                    "SELECT 1 FROM embeddings WHERE model=? AND revision=? LIMIT 1",
                    (model, revision),
                ).fetchone()
            if has_vectors:
                dense_input = query
                if is_short_or_referential and messages:
                    lead = " ".join([str(m.get("text", "")) for m in messages[-2:] if m.get("text")])
                    if lead.strip():
                        dense_input = f"{lead} {query}"
                try:
                    raw_vector = encoder.embed([dense_input[:1000]], mode='query', timeout=0.8)[0]
                except TypeError:
                    raw_vector = encoder.embed([dense_input[:1000]], mode='query')[0]
                dense_vector = _normalise_vector(raw_vector)
                dense_model = model
                dense_revision = str(getattr(encoder, 'revision', revision) or revision)
        except Exception:
            dense_vector = None
            dense_model = None
            dense_revision = None

        def add(row, channel, rank, extra=0.0):
            if not row or not row['id']:
                return
            item = dict(row)
            entry = candidates.get(item['id'])
            if entry is None:
                entry = {'row': item, 'channels': {}, 'score': 0.0, 'dense': -1.0}
                candidates[item['id']] = entry
                if item.get('supersedes_id'):
                    entry['score'] += 0.006
                # Time recency boost only for durable events/commitments (not raw chat or noisy eviction lines)
                recency_age = now - float(item.get('at', 0) or 0)
                if 0 <= recency_age <= 72 * 3600 and item.get('kind') in {'episode', 'commitment'} and item.get('channel') not in {'rolling_eviction', 'bilibili_watch'}:
                    entry['score'] += 0.008 * (1.0 - (recency_age / (72 * 3600)))
            previous = entry['channels'].get(channel)
            if previous is None or rank < previous:
                entry['channels'][channel] = rank
                weight = CHANNEL_WEIGHT.get(channel, 0.85 if channel == 'vertical' else 0.5)
                if item.get('channel') == 'rolling_eviction':
                    weight *= 0.55
                entry['score'] += weight / (RRF_K + max(1, rank))
                if previous is not None:
                    entry['score'] -= weight / (RRF_K + max(1, previous))
            if channel == 'dense' and float(extra) > entry['dense'] and item.get('_dense_chunk'):
                entry['row']['_dense_chunk'] = item['_dense_chunk']
            entry['dense'] = max(entry['dense'], float(extra))

        with self.db() as db:
            lexical_rank = 0
            if query_terms:
                expression = " OR ".join('"' + word + '"' for word in sorted(query_terms)[:40])
                rows = db.execute("SELECT r.* FROM search JOIN records r ON r.id=search.rowid WHERE search MATCH ? AND " + scope + live +
                                  " ORDER BY bm25(search),r.at DESC LIMIT 48", [expression, *params, now]).fetchall()
                for row in rows:
                    lexical_rank += 1
                    add(row, 'lexical', lexical_rank)

            # Independent dense channel: it scans embeddings, not the lexical hits.
            if dense_vector is not None and dense_model is not None:
                try:
                    dense_rank = 0
                    for row, score in self._dense_candidates(
                            db, dense_vector, dense_model, dense_revision or '', scope, params, now):
                        dense_rank += 1
                        add(row, 'dense', dense_rank, extra=score)
                except Exception:
                    pass

            # Latest personal cards, with the same correction shadowing as before.
            cards = list(db.execute("SELECT r.* FROM records r WHERE r.kind='person' AND " + scope + live +
                " AND NOT EXISTS (SELECT 1 FROM records n WHERE n.kind='person' AND n.user_id=r.user_id AND n.key=r.key "
                "AND (n.session=r.session OR n.shared=1 AND r.shared=1) AND (n.at>r.at OR (n.at=r.at AND n.id>r.id))) "
                "ORDER BY r.at DESC LIMIT 40", [*params, now]))
            personal_query = bool(re.search(r"记得|认识|称呼|叫我|名字|昵称|我是谁|印象|记忆|评价|怎么看我|了解|喜欢|偏好|讨厌|在学|研究|专业|方向|学什么|最近在干", query))
            card_rank = 0
            for card in cards:
                key_str = str(card['key'] or '')
                is_focus_card = str(card['user_id'] or '') == focus
                is_focus_portrait = is_focus_card and (key_str.startswith('关系印象·') or key_str == '核心身份')
                overlap = len(terms(index_text(card['text'] or '') + ' ' + key_str) & query_terms)
                relevant = overlap >= 1 or (personal_query and card['user_id'] in users) or is_focus_portrait
                if is_focus_card and key_str == '称呼' and personal_query:
                    relevant = True
                if relevant:
                    card_rank += 1
                    add(card, 'card', card_rank)
                    if is_focus_card and card['id'] in candidates:
                        if personal_query:
                            candidates[card['id']]['score'] += 0.06 if (key_str.startswith('关系印象·') or key_str == '核心身份') else 0.03
                        elif is_focus_portrait:
                            candidates[card['id']]['score'] += 0.025

            pref_rank = 0
            for row in db.execute("SELECT r.* FROM records r WHERE r.kind='preference' AND r.state='active' AND r.session=?" + live +
                                  " ORDER BY r.at DESC LIMIT 6", (session, now)).fetchall():
                overlap = len(terms(index_text(row['text'] or '') + ' ' + row['key']) & query_terms)
                if overlap or personal_query:
                    pref_rank += 1
                    add(row, 'preference', pref_rank)

            # Vertical recall branch: active commitments and shared episodes for target/active users
            vert_users = [focus] + [u for u in users if u != focus][:3]
            vert_rank = 0
            for uid in vert_users:
                if not re.fullmatch(r'[1-9][0-9]{0,19}', str(uid)):
                    continue
                vert_rows = db.execute(
                    "SELECT r.* FROM records r WHERE r.kind IN ('commitment', 'episode') AND r.user_id=? AND " + scope + live +
                    " ORDER BY r.at DESC LIMIT 8", [uid, *params, now]
                ).fetchall()
                for v_row in vert_rows:
                    v_text = index_text(v_row['text'] or v_row['summary'] or '')
                    v_overlap = len(terms(v_text + ' ' + str(v_row['key'] or '')) & query_terms)
                    if v_overlap >= 1 or v_row['kind'] == 'commitment' or uid == focus:
                        vert_rank += 1
                        add(v_row, 'vertical', vert_rank, extra=0.015 if v_overlap >= 1 else 0.0)

            names = list(db.execute("SELECT r.* FROM records r JOIN edges e ON e.record_id=r.id WHERE e.subject=? AND e.relation='self:称呼' AND " + scope +
                " ORDER BY r.at DESC,r.id DESC LIMIT 1", ["person:" + focus, *params]))
            name_cards = list(db.execute("SELECT r.* FROM records r WHERE kind='person' AND user_id=? AND key='称呼' AND " + scope +
                " ORDER BY at DESC,id DESC LIMIT 1", [focus, *params]))
            name_winner = max([*name_cards, *names], key=lambda row: (row['at'], row['id']), default=None)
            if personal_query and names and name_winner and name_winner['id'] == names[0]['id']:
                add(names[0], 'name', 1)
            elif personal_query and name_winner:
                add(name_winner, 'name', 1)

            if re.search(r"记得|认识我|认得我|之前聊|见过我", query):
                recall_rank = 0
                for row in db.execute("SELECT r.* FROM records r WHERE r.kind='chat' AND r.user_id=? AND " + scope +
                                      " ORDER BY r.at DESC LIMIT 8", [focus, *params]).fetchall():
                    recall_rank += 1
                    add(row, 'recall', recall_rank)

            # Graph expansion is bounded to 1-2 hops from the best few seeds.
            seeds = [rid for rid, entry in sorted(candidates.items(), key=lambda pair: pair[1]['score'], reverse=True)[:4]]
            graph_rank = 0
            hop1_nodes = set()
            for rid in seeds:
                topic_objects = [edge[0] for edge in db.execute(
                    "SELECT object FROM edges WHERE record_id=? AND relation='topic' LIMIT 3", (rid,))]
                for object_value in topic_objects:
                    for row in db.execute("SELECT r.* FROM records r JOIN edges e ON e.record_id=r.id "
                                          "WHERE e.relation='topic' AND e.object=? AND " + scope + live +
                                          " ORDER BY r.at DESC LIMIT 3", [object_value, *params, now]).fetchall():
                        if row['id'] == rid:
                            continue
                        graph_rank += 1
                        add(row, 'graph', graph_rank)
                        if rid in seeds[:2]:
                            hop1_nodes.add(row['id'])
                try:
                    related = db.execute(
                        "SELECT e2.subject FROM graph_edges e1 JOIN graph_edges e2 "
                        "ON e2.object=e1.object AND e2.relation=e1.relation "
                        "WHERE e1.subject=? AND e1.relation='discussed_topic' AND e2.subject LIKE 'memory:%' LIMIT 6",
                        ("memory:" + str(rid),)).fetchall()
                    for (subject,) in related:
                        target_id = int(subject.split(':', 1)[1])
                        if target_id == rid:
                            continue
                        row = db.execute("SELECT r.* FROM records r WHERE r.id=? AND " + scope,
                                         [target_id, *params]).fetchone()
                        if row:
                            graph_rank += 1
                            add(row, 'graph', graph_rank)
                            if rid in seeds[:2]:
                                hop1_nodes.add(target_id)
                except (sqlite3.Error, ValueError):
                    pass
                # Handwritten related/supersedes links are first-class bounded recall
                # paths too (scope-checked, active records only).
                try:
                    links = []
                    links.extend(db.execute(
                        "SELECT object AS node FROM graph_edges WHERE subject=? "
                        "AND relation IN ('refers_to','supersedes') AND object LIKE 'memory:%' LIMIT 6",
                        ("memory:" + str(rid),)).fetchall())
                    links.extend(db.execute(
                        "SELECT subject AS node FROM graph_edges WHERE object=? "
                        "AND relation IN ('refers_to','supersedes') AND subject LIKE 'memory:%' LIMIT 6",
                        ("memory:" + str(rid),)).fetchall())
                    seen_links = set()
                    for (link,) in links:
                        if link in seen_links:
                            continue
                        seen_links.add(link)
                        try:
                            target_id = int(link.split(':', 1)[1])
                        except (IndexError, ValueError):
                            continue
                        row = db.execute("SELECT r.* FROM records r WHERE r.id=? AND " + scope + live,
                                         [target_id, *params, now]).fetchone()
                        if row:
                            graph_rank += 1
                            add(row, 'graph', graph_rank)
                            if rid in seeds[:2]:
                                hop1_nodes.add(target_id)
                except sqlite3.Error:
                    pass

            # 2-hop expansion from top seeds' neighbors (bounded to at most 4 additional nodes)
            hop2_count = 0
            for nid in list(hop1_nodes)[:4]:
                if hop2_count >= 4:
                    break
                try:
                    h2_links = db.execute(
                        "SELECT object FROM graph_edges WHERE subject=? "
                        "AND relation IN ('discussed_topic','refers_to','supersedes') AND object LIKE 'memory:%' LIMIT 3",
                        ("memory:" + str(nid),)
                    ).fetchall()
                    for (h2_obj,) in h2_links:
                        try:
                            h2_id = int(h2_obj.split(':', 1)[1])
                        except (IndexError, ValueError):
                            continue
                        if h2_id in candidates or h2_id == nid:
                            continue
                        row = db.execute("SELECT r.* FROM records r WHERE r.id=? AND " + scope + live,
                                         [h2_id, *params, now]).fetchone()
                        if row:
                            graph_rank += 1
                            add(row, 'graph', graph_rank)
                            hop2_count += 1
                            if hop2_count >= 4:
                                break
                except sqlite3.Error:
                    pass

            latest_cards = {row["id"] for row in cards}
            visible = {(session, str(row.get("message_id", ""))) for row in messages}
            result = []
            sources = set()

            # Reserve a bounded slot/token window for linked old notes when the
            # explicit query is relevant (or asks for recall).  Current memory is
            # still rendered first and keeps at least one slot; unused reserved
            # tokens remain available to it.
            total_budget = max(0, int(budget))
            total_limit = max(0, int(limit))
            legacy_lines = []
            legacy_slots = 0
            reserved_budget = 0
            if total_budget > 0 and total_limit > 0:
                reserve_cap = min(420, max(0, total_budget - 160))
                if reserve_cap >= 60:
                    try:
                        from hakimi.social.identity_legacy import retrieve_legacy_notes
                        legacy_lines = retrieve_legacy_notes(
                            self.base, session, focus, messages, target=target,
                            query=query, cross_groups=cross_groups, budget=reserve_cap,
                            limit=min(2, max(0, total_limit - 1)), now=now)
                        legacy_slots = min(len(legacy_lines), max(0, total_limit - 1))
                        reserved_budget = sum(len(line) for line in legacy_lines)
                    except Exception:
                        legacy_lines = []
                        legacy_slots = 0
                        reserved_budget = 0
            budget = max(0, total_budget - reserved_budget)
            bili_explicit = bool(re.search(r"b站|哔哩哔哩|bilibili|视频|直播|看片|up主|番剧|弹幕", query, re.I))

            def render(entry):
                row = entry['row']
                if str(session).startswith('group:') and (
                    row.get('channel') == 'desktop_pet' or row.get('key') in {'桌宠实时想法', '桌宠阶段脉络'}
                ):
                    if _DESKTOP_PRIVATE_LEAK_RE.search(str(row.get('text') or '') + ' ' + str(row.get('evidence') or '')):
                        return None
                if row['at'] > now or row['expires'] and row['expires'] <= now:
                    return None
                if row.get('session') == 'self:bilibili' and session != 'self:bilibili' and not bili_explicit:
                    bili_ov = (terms(index_text(str(row.get('text') or '') + ' ' + str(row.get('key') or ''))) & query_terms) - _BILI_GENERIC_TERMS
                    if entry['dense'] < 0.58 and len(bili_ov) < 2:
                        return None
                if row['kind'] == 'lore' and row['source'] in sources:
                    return None
                if row['kind'] == 'chat':
                    for edge in db.execute("SELECT relation FROM edges WHERE record_id=? AND relation LIKE 'self:%'", (row['id'],)):
                        newer = db.execute("SELECT 1 FROM records r WHERE r.kind='person' AND r.user_id=? AND r.key=? AND r.at>? AND " + scope,
                                           [row['user_id'], edge[0][5:], row['at'], *params]).fetchone()
                        if newer:
                            return None
                if row['kind'] not in {'chat', 'person'} and row['key'] and db.execute(
                        "SELECT 1 FROM records WHERE kind=? AND session=? AND key=? AND state='active' AND (at>? OR (at=? AND id>?)) LIMIT 1",
                        (row['kind'], row['session'], row['key'], row['at'], row['at'], row['id'])).fetchone():
                    return None
                if row["kind"] == "person" and (row["id"] not in latest_cards or
                        name_winner and row['user_id'] == focus and row["key"] == "称呼" and name_winner['id'] != row['id']):
                    return None
                query_clean = index_text(query).strip()
                if row["kind"] == "chat" and (
                        (row["session"], str(row["origin"])) in visible
                        or (row["session"] == session and abs(now - float(row["at"] or 0)) <= 180
                            and query_clean and index_text(row["text"] or "").strip() == query_clean)
                        or name_winner and row["user_id"] == focus and row["id"] != name_winner["id"] and
                        db.execute("SELECT 1 FROM edges WHERE record_id=? AND relation='self:称呼'", (row["id"],)).fetchone()):
                    return None
                raw_text = row["text"] or row['summary']
                chunk = row.get('_dense_chunk') if row["kind"] != "person" else None
                # Chunk offsets refer to the original, before index_text removes URLs/CQ.
                if chunk:
                    raw_text = raw_text[chunk['start_offset']:chunk['end_offset']]
                text = index_text(raw_text).strip()
                if not text:
                    return None
                lowered = text.lower()
                positions = [lowered.find(term) for term in query_terms if term in lowered]
                start = 0 if row["kind"] == "person" else max(0, min(positions, default=0) - 20)
                max_len = 240 if row["kind"] == "person" else 220 if row["kind"] != "chat" else 100
                excerpt = ("…" if start or chunk and chunk["start_offset"] else "") + text[start:start + max_len]
                if row["kind"] == "person":
                    label = "人物" + row["user_id"] + "·" + row["key"]
                elif row["kind"] == "chat":
                    label = ("本群" if row["session"] == session else "他群账号" + row["user_id"]) + (
                        "原话" if row['text'] else "要点")
                elif row["kind"] == "lore":
                    label = '资料'
                elif row["kind"] == "episode":
                    if row["channel"] == "daily_events":
                        label = '日常'
                    elif row["channel"] == "bilibili_watch":
                        label = 'B站亲历'
                    else:
                        label = '互动'
                elif row["kind"] == "reflection":
                    label = "反思"
                elif row["kind"] == "commitment":
                    label = "约定"
                else:
                    label = "本群偏好"
                if explain:
                    label += "·命中:" + "+".join(sorted(entry['channels']))
                recorded = '' if row['kind'] == 'chat' else '记'
                line = f"[M{row['id']}] {label}·{recorded}{stamp(row['at'], now=now)}：{excerpt}"
                if row['session'] != session and row['session'] != 'self:bilibili':
                    line += '\n来源：' + row['session']
                if row["source"]:
                    line += "\n出处：" + row["source"]
                return line

            bili_count = 0
            for _, entry in sorted(candidates.items(), key=lambda pair: (pair[1]['score'], pair[1]['row']['at'], pair[1]['row']['id']), reverse=True):
                is_cross_bili = (entry['row'].get('session') == 'self:bilibili' and session != 'self:bilibili')
                if is_cross_bili and not bili_explicit and bili_count >= 2:
                    continue
                line = render(entry)
                if not line:
                    continue
                if len(line) > budget:
                    continue
                result.append(line)
                if is_cross_bili:
                    bili_count += 1
                budget -= len(line)
                if entry['row']['kind'] == 'lore':
                    sources.add(entry['row']['source'])
                if len(result) >= max(0, total_limit - legacy_slots):
                    break
            # Append the pre-reserved linked legacy notes within the same total
            # budget/limit.  Their compact labels leave the full mapping to the
            # read metadata, and current-runtime memories already kept a slot.
            if legacy_lines:
                remaining = total_budget - sum(len(line) for line in result)
                for line in legacy_lines:
                    if len(line) > remaining:
                        continue
                    result.append(line)
                    remaining -= len(line)
                    if len(result) >= total_limit:
                        break
            from hakimi.memory.voice_receipts import search as voice_search
            voice_hits=voice_search(self.base,session,query,limit=min(2,total_limit))
            voice_lines=[f"[语音:{hit['message_id']}] 本会话自动转写（可能有误，voice.read读原文）：{hit['text']}" for hit in voice_hits]
            combined=[];remaining=total_budget
            for line in voice_lines+result:
                if len(line)<=remaining and len(combined)<total_limit:combined.append(line);remaining-=len(line)
            return combined
