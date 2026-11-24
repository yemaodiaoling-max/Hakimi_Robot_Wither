"""Turn-local retrieval budget and progressive, authorized tool reuse."""
import json
import re


class MemoryRetrieval:
    names = frozenset({'memory.search', 'memory.read', 'memory.graph', 'context.read'})
    max_rounds = 8

    def __init__(self):
        self.rounds = 0
        self.cache = {}
        self.searches = {}
        self.reads = {}
        self.allowed = True
        self.read_ends = {}
        self.seen_memories = set()

    @property
    def exhausted(self):
        return self.rounds >= self.max_rounds

    def begin(self, parts):
        """Count model retrieval batches, including repeated/cached requests."""
        retrieval = any(p.get('functionCall', {}).get('name') in self.names for p in parts)
        self.allowed = not self.exhausted
        if retrieval and self.allowed:
            self.rounds += 1

    def run(self, name, arguments, execute):
        if name not in self.names:
            return execute(arguments)
        if not self.allowed:
            return {'error': 'retrieval_round_limit', 'retrieval_rounds': self.rounds,
                    'message': '本轮检索已结束，请使用已有资料完成回复。'}
        args = dict(arguments)
        if name == 'memory.search':
            key = (str(args.get('query', '')).strip().casefold(), args.get('scope', 'current'))
            attempt = self.searches.get(key, 0)
            self.searches[key] = attempt + 1
            # Scope never expands implicitly: only the amount of material grows.
            args['limit'] = (3, 6, 12)[min(attempt, 2)]
        read_key = None
        if name == 'memory.read':
            key = (str(args.get('memory_id', '')).upper(), args.get('source', 'note'),
                   args.get('offset', 0))
            attempt = self.reads.get(key, 0)
            self.reads[key] = attempt + 1
            if 'limit' not in args:
                args['limit'] = 900 if not attempt else 3000
            read_key = key
            start = max(0, int(args.get('offset') or 0))
            end = start + max(1, min(6000, int(args['limit'] or 3000)))
            delivered = self.read_ends.get(key, start)
            if delivered >= end:
                return self.reused(name, args)
            args['offset'] = delivered
            args['limit'] = end - delivered
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in self.cache:
            return self.reused(name, args)
        output = execute(args)
        if not output.get('error') and output.get('ok', True):
            # qq_cli returns a JSON envelope inside stdout; test runners may
            # return the envelope directly. Keep all provenance and page fields.
            wrapped = isinstance(output.get('stdout'), str)
            try:
                envelope = json.loads(output['stdout']) if wrapped else output
            except (ValueError, TypeError):
                return output
            if not isinstance(envelope, dict) or envelope.get('error') or not envelope.get('ok', True):
                return output
            result = envelope.get('result', envelope)
            if isinstance(result, dict) and (result.get('error') or not result.get('ok', True)):
                return output
            self.cache[key] = True
            if isinstance(result, dict):
                if read_key is not None and isinstance(result.get('text'), str):
                    self.read_ends[read_key] = result.get('offset', args['offset']) + len(result['text'])
                    if result.get('next_offset') is None:
                        self.read_ends[read_key] = float('inf')
                if name == 'memory.search' and isinstance(result.get('memories'), list):
                    fresh, reused = [], 0
                    for memory in result['memories']:
                        if isinstance(memory, str):
                            signature = re.sub(r'·命中:[^·\n]*', '', memory)
                        else:
                            stable = {k: v for k, v in memory.items()
                                      if k not in {'explain', 'rank', 'score', 'channels'}}
                            signature = json.dumps(stable, sort_keys=True, ensure_ascii=False)
                        if signature in self.seen_memories:
                            reused += 1
                        else:
                            self.seen_memories.add(signature)
                            fresh.append(memory)
                    result['memories'] = fresh
                    if reused:
                        result['already_returned_count'] = reused
                if wrapped:
                    output = dict(output, stdout=json.dumps(envelope, ensure_ascii=False))
        return output

    @staticmethod
    def reused(name, args):
        return {'ok': True, 'already_retrieved': True, 'tool': name,
                'arguments': args, 'message': '相同范围的资料已在本轮前文返回，请复用。'}
