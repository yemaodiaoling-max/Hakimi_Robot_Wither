"""Versioned, bounded JSON digest protocol. Checksums provide integrity, not identity.

Endpoint identity must come from the configured authenticated transport, not JSON.
All text is untrusted data; imported digests are never native observations.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import re

VERSION = 1
MAX_PACKET_BYTES = 32_768
MAX_FACTS = 12
MAX_TEXT = 1600
MAX_LIFETIME = 7 * 86400
ATTRIBUTIONS = {'observation', 'hearsay', 'inference', 'feeling'}
EXPORT_KINDS = {'watch_note', 'public_fact', 'episode', 'preference', 'reflection'}
SENSITIVE = re.compile(r'(?i)(?:cookie\s*[:=]|sessdata|bili_jct|authorization\s*[:=]|'
                       r'api[_ -]?key\s*[:=]|password\s*[:=]|-----BEGIN .*PRIVATE KEY|'
                       r'access[_ -]?token\s*[:=]|refresh[_ -]?token\s*[:=]|'
                       r'(?:secret|token|passphrase|credential)\s*[:=]|(?:密码|密钥|口令)\s*[:：=为是])')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def text(value, limit, *, empty=False):
    if (type(value) is not str or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 and c not in '\n\t' for c in value)):
        raise ValueError('invalid bounded string')


def number(value, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError('invalid finite number')


def revision(value):
    if type(value) is not int or not 1 <= value <= 2**53:
        raise ValueError('invalid revision')


def coverage_valid(coverage):
    if type(coverage) not in (list, tuple) or len(coverage) > 16:
        raise ValueError('invalid coverage')
    for interval in coverage:
        if type(interval) is not dict or set(interval) != {'start', 'end', 'modality'}:
            raise ValueError('invalid coverage interval')
        number(interval['start'])
        number(interval['end'])
        if interval['end'] <= interval['start'] or interval['modality'] not in {'audio', 'video', 'subtitle', 'mixed'}:
            raise ValueError('invalid media interval')


def references_valid(refs):
    if type(refs) not in (list, tuple) or not 1 <= len(refs) <= 8:
        raise ValueError('source references required')
    for ref in refs:
        text(ref, 240)
        if SENSITIVE.search(ref):
            raise ValueError('credential-like reference')


@dataclass(frozen=True)
class SourceItem:
    source_id: str
    revision: int
    scope: str
    kind: str
    text: str
    created_at: float
    importance: float = 0
    attribution: str = 'inference'
    source_refs: tuple[str, ...] = ()
    coverage: tuple[dict, ...] = ()
    imported: bool = False
    private: bool = False
    pre_truncated: bool = False

    def __post_init__(self):
        text(self.source_id, 160)
        revision(self.revision)
        text(self.scope, 160)
        text(self.kind, 40)
        text(self.text, 32_000)
        number(self.created_at)
        number(self.importance)
        if self.importance > 1 or self.attribution not in ATTRIBUTIONS:
            raise ValueError('invalid source assessment')
        if type(self.imported) is not bool or type(self.private) is not bool or type(self.pre_truncated) is not bool:
            raise ValueError('invalid source flags')
        coverage_valid(self.coverage)
        # Excluded records may have no references; they never reach a packet.
        if self.source_refs:
            references_valid(self.source_refs)


@dataclass(frozen=True)
class SourceBatch:
    items: tuple[SourceItem, ...]
    checkpoint: str

    def __post_init__(self):
        text(self.checkpoint, 512, empty=True)
        if type(self.items) not in (tuple, list) or len(self.items) > 1000 or any(type(x) is not SourceItem for x in self.items):
            raise ValueError('invalid source batch')


def seal_packet(packet):
    """Copy and checksum; never modifies caller payload."""
    body = {key: value for key, value in packet.items() if key != 'checksum'}
    body['checksum'] = digest(body)
    return json.loads(canonical(body))


def validate_packet(packet, *, now=None):
    expected = {'version', 'packet_id', 'origin', 'recipient', 'origin_id', 'recipient_id',
                'kind', 'created_at', 'expires_at', 'source_ids', 'facts', 'checksum'}
    if type(packet) is not dict or set(packet) != expected:
        raise ValueError('invalid packet fields')
    raw = canonical(packet).encode('utf-8')
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError('packet too large')
    if type(packet['version']) is not int or packet['version'] != VERSION:
        raise ValueError('unsupported packet version')
    for key in ('packet_id', 'origin_id', 'recipient_id'):
        text(packet[key], 160)
    for key in ('origin', 'recipient', 'kind'):
        text(packet[key], 40)
    if packet['origin'] not in {'qq', 'bilibili'} or packet['recipient'] not in {'qq', 'bilibili'} or packet['origin'] == packet['recipient']:
        raise ValueError('invalid endpoints')
    expected_kind = 'watch_digest' if packet['origin'] == 'bilibili' else 'qq_digest'
    if packet['kind'] not in {expected_kind, 'retraction'}:
        raise ValueError('origin/kind mismatch')
    number(packet['created_at'])
    number(packet['expires_at'])
    lifetime = packet['expires_at']-packet['created_at']
    if not 0 < lifetime <= MAX_LIFETIME:
        raise ValueError('invalid expiry')
    if now is not None:
        number(now)
        if packet['created_at'] > now + 60 or packet['expires_at'] <= now:
            raise ValueError('expired or future packet')
    sources, facts = packet['source_ids'], packet['facts']
    if type(sources) is not list or not 1 <= len(sources) <= MAX_FACTS or type(facts) is not list:
        raise ValueError('invalid source/fact list')
    source_keys = []
    for source in sources:
        if type(source) is not dict or set(source) != {'source_id', 'revision', 'scope'}:
            raise ValueError('invalid source identity')
        text(source['source_id'], 160)
        text(source['scope'], 160)
        if source['scope'] not in {'self', 'public'}:
            raise ValueError('non-shareable source scope')
        revision(source['revision'])
        source_keys.append((source['scope'], source['source_id'], source['revision']))
    if len(set(source_keys)) != len(source_keys):
        raise ValueError('duplicate source revision')
    if packet['kind'] == 'retraction':
        if facts:
            raise ValueError('retraction has facts')
    else:
        if len(facts) != len(sources):
            raise ValueError('facts/source mismatch')
        for fact, identity in zip(facts, sources):
            if type(fact) is not dict or set(fact) != {
                'source_id', 'revision', 'scope', 'text', 'source_refs', 'coverage',
                'attribution', 'created_at', 'importance', 'truncated'}:
                raise ValueError('invalid fact schema')
            if any(fact[k] != identity[k] for k in identity):
                raise ValueError('mismatched provenance')
            text(fact['text'], MAX_TEXT)
            if SENSITIVE.search(fact['text']):
                raise ValueError('credential-like fact')
            references_valid(fact['source_refs'])
            coverage_valid(fact['coverage'])
            number(fact['created_at'])
            number(fact['importance'])
            if fact['created_at'] > packet['created_at'] or fact['importance'] > 1:
                raise ValueError('invalid fact time/importance')
            if type(fact['truncated']) is not bool or fact['attribution'] not in ATTRIBUTIONS:
                raise ValueError('invalid fact annotation')
            if packet['kind'] == 'watch_digest' and (not fact['coverage'] or not any(
                ref.startswith(('bilibili:video:', 'bilibili:live:')) for ref in fact['source_refs'])):
                raise ValueError('watch digest requires native media source and actual coverage')
    if type(packet['checksum']) is not str or packet['checksum'] != digest({k:v for k,v in packet.items() if k != 'checksum'}):
        raise ValueError('checksum mismatch')
    return packet
