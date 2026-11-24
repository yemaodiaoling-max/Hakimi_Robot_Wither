"""Operator-activated, model-specific capability and conservative USD price gate."""
from dataclasses import dataclass
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import time
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from .gateway import CognitionUnavailable, _NoRedirect


ORIGINAL_RELAY_URL = 'https://blameupstream.com/v1'
FLASH_TARIFF_URL = 'https://blameupstream.com/api/v1/model-plaza'
FLASH_TARIFF_REFRESH = 86400
FLASH_TARIFF_MAX_AGE = 3 * FLASH_TARIFF_REFRESH
FLASH_TARIFF_RETRY = 900


def amount(value):
    try:result=Decimal(str(value))
    except (InvalidOperation,ValueError):raise CognitionUnavailable('provider_price_invalid') from None
    if not result.is_finite() or result<0 or result>1_000_000:
        raise CognitionUnavailable('provider_price_invalid')
    return result


def reviewed_date(value,now):
    try:valid=date.fromisoformat(value)
    except (TypeError,ValueError):raise CognitionUnavailable('provider_review_date_invalid') from None
    if datetime.fromtimestamp(now,timezone.utc).date()>valid:
        raise CognitionUnavailable('provider_review_expired')
    return valid


@dataclass(frozen=True)
class ActivatedProfile:
    provider: str
    base_url: str
    api_key_file: str
    observer_model: str
    decider_model: str
    raw: dict
    market_rates: tuple[Decimal, Decimal] | None = None

    def prices(self,model,now):
        if self.market_rates is not None and model == self.observer_model:
            return self.market_rates
        if self.raw.get('price_source')!='operator_tariff' or self.raw.get('review_valid_until'):
            reviewed_date(self.raw.get('review_valid_until'),now)
        table = self.raw.get('prices')
        price = table.get(model) if isinstance(table, dict) else None
        if type(price) is not dict:raise CognitionUnavailable('provider_model_price_missing')
        incoming=amount(price.get('input_usd_per_million'))
        outgoing=amount(price.get('output_usd_per_million'))
        if incoming+outgoing<=0:raise CognitionUnavailable('provider_price_invalid')
        if price.get('billing_currency')!='USD':
            raise CognitionUnavailable('provider_billing_currency_unreviewed')
        if not isinstance(price.get('source'),str) or not 1<=len(price['source'])<=512:
            raise CognitionUnavailable('provider_price_source_missing')
        original=price.get('source_price')
        if type(original) is not dict:raise CognitionUnavailable('provider_source_price_missing')
        source_input=amount(original.get('input_per_million'))
        source_output=amount(original.get('output_per_million'))
        if incoming<source_input or outgoing<source_output:
            raise CognitionUnavailable('provider_usd_bound_underpriced')
        modalities=price.get('input_modalities')
        expected = {'text', 'image', 'audio', 'video'}
        if self.raw.get('media_mode') == 'text_only':
            expected = {'text'}
        elif self.raw.get('media_mode') == 'frames_transcript':
            expected = {'text', 'image'}
        if type(modalities) is not list or any(type(x) is not str for x in modalities) or set(modalities)!=expected:
            raise CognitionUnavailable('provider_multimodal_price_missing')
        return incoming,outgoing

    @staticmethod
    def budgets(config, *, monthly_cap=60):
        monthly=amount(config.get('monthly_usd','10'))
        daily=amount(config.get('daily_usd','1'))
        if not 0<monthly<=monthly_cap or not 0<daily<=6:
            raise ValueError('activated provider budget exceeds authorized limit')
        return str(monthly),str(daily)


def load_profile(path,*,clock=time.time,audit_costs=True,_market_rates=None,text_only=False,
                 _allow_text_only=False,_allow_pending_review=False):
    try:raw=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise CognitionUnavailable('provider_profile_unavailable') from None
    if type(raw) is not dict or raw.get('version')!=1 or raw.get('activated') is not True:
        raise CognitionUnavailable('provider_not_activated')
    if raw.get('provider')!='openai_compatible':raise CognitionUnavailable('provider_unsupported')
    for field in ('usage_panel_actual_cost','payload_token_envelope'):
        if type(raw.get(field,False)) is not bool:
            raise CognitionUnavailable('provider_accounting_configuration_invalid')
    if type(raw.get('send_json_mode',True)) is not bool:
        raise CognitionUnavailable('provider_output_format_invalid')
    if audit_costs and raw.get('ledger_currency')!='USD':raise CognitionUnavailable('provider_ledger_currency_mismatch')
    bound=raw.get('input_token_upper_bound')
    if type(bound) is not int or not 1<=bound<=200_000:
        raise CognitionUnavailable('provider_input_bound_unverified')
    media_mode = raw.get('media_mode')
    media_modes = {'text_only'} if text_only else {'frames_audio','frames_transcript','native_video','mp4_image_url'}
    if _allow_text_only:
        media_modes.add('text_only')
    if media_mode not in media_modes:
        raise CognitionUnavailable('provider_media_mode_unverified')
    if audit_costs:
        request_limit=amount(raw.get('per_request_usd_limit','.02'))
        cap=Decimal('.20') if raw.get('usage_panel_actual_cost') is True else Decimal('.10')
        if not 0<request_limit<=cap:
            raise CognitionUnavailable('provider_request_limit_invalid')
    limit_names={'max_output_tokens'} if raw.get('wire_protocol')=='responses' else {'max_completion_tokens','max_tokens'}
    if raw.get('output_limit_parameter') not in limit_names:
        raise CognitionUnavailable('provider_output_cap_unverified')
    maximum=raw.get('max_output_tokens')
    if type(maximum) is not int or not 128<=maximum<=(8192 if media_mode=='text_only' else 2048):
        raise CognitionUnavailable('provider_output_cap_unverified')
    if raw.get('reasoning_effort') not in {None,'low','medium','high'}:
        raise CognitionUnavailable('provider_reasoning_setting_invalid')
    single_pass=raw.get('single_pass',False)
    if type(single_pass) is not bool or type(raw.get('child_roles_enabled')) is not bool:
        raise CognitionUnavailable('provider_supervisor_configuration_invalid')
    if (single_pass and raw['child_roles_enabled']) or (not single_pass and not raw['child_roles_enabled']):
        raise CognitionUnavailable('provider_supervisor_configuration_invalid')
    if raw.get('supervisor_model')!=raw.get('observer_model') or raw.get('decider_model')!=raw.get('observer_model'):
        raise CognitionUnavailable('provider_supervisor_configuration_invalid')
    try:
        url=raw['base_url'];parsed=urlsplit(url)
        if (not isinstance(url,str) or parsed.scheme!='https' or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(c)<33 for c in url) or '\\' in url):
            raise ValueError()
        parsed.port
    except (KeyError,TypeError,ValueError):raise CognitionUnavailable('provider_url_invalid') from None
    for field in ('observer_model','decider_model'):
        if not isinstance(raw.get(field),str) or not re.fullmatch('[A-Za-z0-9_.:/-]{1,160}',raw[field]):
            raise CognitionUnavailable('provider_model_invalid')
    key_file=raw.get('api_key_file')
    if not isinstance(key_file,str) or not key_file or not Path(key_file).is_absolute():
        raise CognitionUnavailable('provider_key_path_invalid')
    now=clock()
    if type(now) not in (int,float) or not math.isfinite(now):raise ValueError('invalid clock')
    verification=raw.get('verification')
    if type(verification) is not dict:raise CognitionUnavailable('provider_capabilities_unverified')
    for model in {raw['observer_model'],raw['decider_model']}:
        proof=verification.get(model)
        if type(proof) is not dict:
            raise CognitionUnavailable('provider_capabilities_unverified')
        audio_proof='speech_transcript' if raw['media_mode']=='frames_transcript' else 'audio'
        required = ('structured_output', 'input_bound', 'output_token_cap')
        if media_mode != 'text_only':
            required += (audio_proof, 'video')
        if audit_costs:
            required += ('usage',)
        pending = _allow_pending_review and proof.get('status') == 'awaiting_first_success'
        if not pending and any(proof.get(field) is not True for field in required):
            raise CognitionUnavailable('provider_capabilities_unverified')
        if proof.get('base_url')!=url.rstrip('/'):
            raise CognitionUnavailable('provider_verification_origin_mismatch')
        if proof.get('output_limit_parameter')!=raw['output_limit_parameter']:
            raise CognitionUnavailable('provider_output_cap_parameter_mismatch')
        if proof.get('media_mode')!=raw['media_mode']:
            raise CognitionUnavailable('provider_media_mode_verification_mismatch')
        if proof.get('max_output_tokens')!=maximum:
            raise CognitionUnavailable('provider_output_cap_parameter_mismatch')
        at=proof.get('verified_at')
        if pending:
            if (media_mode != 'text_only' or proof.get('input_token_upper_bound') != bound
                    or at is not None or any(proof.get(field) is True for field in required)):
                raise CognitionUnavailable('provider_pending_review_contract_invalid')
        elif type(at) not in (int,float) or not math.isfinite(at) or not 0<at<=now+60:
            raise CognitionUnavailable('provider_verification_time_invalid')
    market_rates = _market_rates(raw, now) if _market_rates is not None else None
    if audit_costs and market_rates is None and type(raw.get('prices')) is not dict:raise CognitionUnavailable('provider_model_price_missing')
    profile=ActivatedProfile(raw['provider'],url.rstrip('/'),key_file,raw['observer_model'],raw['decider_model'],raw,market_rates)
    if audit_costs:
        for model in {profile.observer_model,profile.decider_model}:
            incoming,_=profile.prices(model,now)
            if market_rates is None and raw.get('cached_input_rate') is not None and amount(raw['cached_input_rate'])>incoming:
                raise CognitionUnavailable('provider_price_invalid')
    return profile


def load_video_selection_profile(config, *, clock=time.time):
    return load_profile(config['video_selection_profile'], clock=clock, text_only=True)


def load_qq_flash_low_profile(config, *, clock=time.time):
    from .flash_duty import FLASH_LOW_MODEL

    selected = config.get('qq_flash_low_profile')
    if not isinstance(selected, str) or not Path(selected).is_absolute():
        raise CognitionUnavailable('companion_duty_profile_invalid')
    profile = load_profile(selected, clock=clock, text_only=True)
    if (profile.observer_model != FLASH_LOW_MODEL or profile.base_url != ORIGINAL_RELAY_URL
            or profile.raw.get('reasoning_effort') != 'low' or not profile.raw['single_pass']
            or profile.raw.get('wire_protocol', 'openai') != 'openai'):
        raise CognitionUnavailable('companion_duty_profile_invalid')
    return profile


def load_duty_profile(config, *, qq_text=False, review=False, review_haiku=False,
                      clock=time.time, audit_costs=True):
    from .flash_duty import FLASH_MODEL, HAIKU_MODELS, HYDROGEN_MODELS, SONNET_MODELS

    review = review or review_haiku
    if review_haiku:
        selected, models = config.get('qq_review_haiku_profile'), HAIKU_MODELS
    elif review:
        selected, models = config.get('qq_review_sonnet_profile'), SONNET_MODELS
    elif qq_text:
        selected, models = config.get('qq_hydrogen_profile'), HYDROGEN_MODELS
    else:
        selected, models = config.get('flash_duty_profile'), {FLASH_MODEL}
    if not isinstance(selected, str):
        raise CognitionUnavailable('companion_duty_profile_invalid')
    path = Path(selected)
    if not path.is_absolute():
        raise CognitionUnavailable('flash_duty_profile_invalid')

    def market_rates(raw, now):
        model = raw['observer_model']
        protocol = raw.get('wire_protocol', 'openai')
        if (model not in models or raw['base_url'].rstrip('/') != ORIGINAL_RELAY_URL
                or (not qq_text and not review and raw['media_mode'] != 'mp4_image_url')
                or not raw.get('single_pass')
                or protocol not in ({'openai', 'anthropic'} if review else {'openai'})
                or (protocol == 'anthropic' and (raw['output_limit_parameter'] != 'max_tokens'
                                                or raw.get('reasoning_effort') is not None))):
            raise CognitionUnavailable('companion_duty_profile_invalid')
        if not audit_costs or ((qq_text or review) and raw.get('price_source') == 'operator_tariff'):
            return None
        return _cached_flash_tariff(config['state_dir'], model, now)

    profile = load_profile(path, clock=clock, audit_costs=audit_costs, _market_rates=market_rates,
                           text_only=review, _allow_text_only=qq_text, _allow_pending_review=review)
    if review_haiku:
        sonnet = load_duty_profile(config, review=True, clock=clock, audit_costs=False)
        if Path(profile.api_key_file) != Path(sonnet.api_key_file):
            raise CognitionUnavailable('companion_review_account_mismatch')
    return profile


def load_flash_profile(config, *, clock=time.time):
    return load_duty_profile(config, clock=clock)


def _public_flash_tariff(model):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(Request(FLASH_TARIFF_URL, headers={'Accept':'application/json'}), timeout=5) as response:
        if response.geturl() != FLASH_TARIFF_URL:
            raise CognitionUnavailable('flash_tariff_unverified')
        body = response.read(4 * 1024 * 1024 + 1)
    try:
        if len(body) > 4 * 1024 * 1024:
            raise ValueError()
        groups = json.loads(body, parse_float=Decimal)['data']['groups']
        if type(groups) is not list or any(type(group['models']) is not list for group in groups):
            raise ValueError()
        rows = [row['pricing'] for group in groups for row in group['models']
                if type(row) is dict and row.get('name') == model]
        if not rows:
            raise ValueError()
        incoming = outgoing = Decimal(0)
        for pricing in rows:
            if (pricing['billing_mode'] != 'token' or pricing.get('currency', 'USD') != 'USD'
                    or pricing['per_request_price'] not in (None, 0)
                    or pricing['intervals'] != [] or pricing['image_input_price'] not in (None, 0)
                    or pricing['image_output_price'] not in (None, 0)):
                raise ValueError()
            input_rates = [amount(pricing['input_price'])]
            input_rates.extend(amount(pricing[key]) for key in (
                'cache_read_price', 'cache_write_price', 'cache_write_1h_price', 'cache_write_12h_price')
                if pricing.get(key) is not None)
            incoming = max(incoming, amount(max(input_rates) * 1_000_000))
            outgoing = max(outgoing, amount(amount(pricing['output_price']) * 1_000_000))
        if incoming + outgoing <= 0:
            raise ValueError()
        return incoming, outgoing
    except (KeyError, TypeError, ValueError, ArithmeticError, CognitionUnavailable):
        raise CognitionUnavailable('flash_tariff_unverified') from None


def _cached_flash_tariff(state_dir, model, now):
    from ..runtime.storage import Database

    database = Database(Path(state_dir) / 'watcher.sqlite3')
    with database.connect(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS flash_tariff (
            model TEXT PRIMARY KEY, checked_at REAL, next_check REAL, input_rate TEXT, output_rate TEXT)''')
        db.execute('INSERT OR IGNORE INTO flash_tariff VALUES (?,0,0,NULL,NULL)', (model,))
        row = db.execute('SELECT * FROM flash_tariff WHERE model=?', (model,)).fetchone()
        refresh = now >= row['next_check']
        if refresh:
            db.execute('UPDATE flash_tariff SET next_check=? WHERE model=?', (now+FLASH_TARIFF_RETRY, model))
    if refresh:
        try:
            rates = _public_flash_tariff(model)
        except (OSError, HTTPException):
            # Retain last verified prices through a short outage.
            pass
        except CognitionUnavailable:
            # A reachable source announcing an unverified tariff invalidates the cache.
            with database.connect(write=True) as db:
                db.execute('UPDATE flash_tariff SET input_rate=NULL, output_rate=NULL WHERE model=?', (model,))
            raise
        else:
            with database.connect(write=True) as db:
                db.execute('UPDATE flash_tariff SET checked_at=?, next_check=?, input_rate=?, output_rate=? WHERE model=?',
                           (now, now+FLASH_TARIFF_REFRESH, str(rates[0]), str(rates[1]), model))
            return rates
    if row['input_rate'] is not None and 0 <= now-row['checked_at'] <= FLASH_TARIFF_MAX_AGE:
        return Decimal(row['input_rate']), Decimal(row['output_rate'])
    raise CognitionUnavailable('flash_tariff_unavailable')
