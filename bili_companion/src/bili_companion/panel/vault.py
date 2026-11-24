"""Windows user-bound DPAPI vault, or short-lived in-memory storage elsewhere."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

PHASES={'empty','pending','probing','models_ready','probe_failed','testing','verified','active'}
PUBLIC_FIELDS={'protocol','model_ids','selected_model','message_code','video_verified',
               'audio_verified','usage_verified','pricing_required','budget_usd','http_status','live_config_changed'}


def validate_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(c)<33 for c in value):
        raise ValueError('invalid_url')
    url=urlsplit(value)
    if (url.scheme not in {'http','https'} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or '\\' in value):
        raise ValueError('invalid_url')
    try:port=url.port
    except ValueError:raise ValueError('invalid_url') from None
    if port is not None and not 1<=port<=65535:raise ValueError('invalid_url')
    return urlunsplit((url.scheme,url.netloc,url.path.rstrip('/'),'',''))


def dpapi(data,*,decrypt=False):
    if os.name!='nt':raise OSError('DPAPI unavailable')
    class Blob(ctypes.Structure):
        _fields_=[('length',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    result=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        call=crypt.CryptUnprotectData
        call.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        okay=call(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result))
    else:
        call=crypt.CryptProtectData
        call.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        okay=call(ctypes.byref(source),'Bilibili provider setup',None,None,None,1,ctypes.byref(result))
    if not okay:raise OSError('DPAPI operation failed')
    try:return ctypes.string_at(result.data,result.length)
    finally:kernel.LocalFree(ctypes.cast(result.data,ctypes.c_void_p))


class LocalVault:
    def __init__(self,private_dir,*,memory_only=False):
        self.path=Path(private_dir).resolve()
        self.memory_only=memory_only or os.name!='nt'
        self._submission=None;self._status=None;self.lock=threading.RLock()

    def _write(self,name,data):
        self.path.mkdir(parents=True,exist_ok=True,mode=0o700)
        target=self.path/name;temp=target.with_name(target.name+'.tmp')
        with open(temp,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.chmod(temp,0o600);os.replace(temp,target)

    def save_submission(self,base_url,api_key,protocol='auto',*,insecure_http_confirmed=False):
        url=validate_url(base_url)
        if (not isinstance(api_key,str) or not 1<=len(api_key)<=8192
                or any(ord(c)<33 or ord(c)>126 for c in api_key)):
            raise ValueError('invalid_key')
        if protocol not in {'auto','gemini','openai'}:raise ValueError('invalid_protocol')
        with self.lock:
            submission=dict(submission_id=uuid.uuid4().hex,base_url=url,api_key=api_key,protocol=protocol,
                insecure_http_confirmed=insecure_http_confirmed is True,submitted_at=time.time())
            if not self.memory_only:
                self._write('submission.dpapi',dpapi(json.dumps(submission,allow_nan=False).encode()))
            self._submission=submission
            self._status=dict(submission_id=submission['submission_id'],phase='pending',tls=url.startswith('https://'),
                storage='memory' if self.memory_only else 'windows_dpapi',updated_at=time.time(),
                live_config_changed=False,budget_usd=10)
            if not self.memory_only:self._write('status.json',json.dumps(self._status).encode())
            return submission['submission_id']

    def read_submission(self):
        """For local probe code only. Never serialize this return value to logs/UI."""
        with self.lock:
            if not self.memory_only:
                if not (self.path/'submission.dpapi').is_file():return None
                value=json.loads(dpapi((self.path/'submission.dpapi').read_bytes(),decrypt=True))
                validate_url(value['base_url'])
                return value
            return dict(self._submission) if self._submission else None

    def public_status(self):
        with self.lock:
            if not self.memory_only and (self.path/'status.json').is_file():
                value=json.loads((self.path/'status.json').read_text())
            else:value=self._status
            if not value:return dict(phase='empty',storage='memory' if self.memory_only else 'windows_dpapi',budget_usd=10,live_config_changed=False)
            # Persisted files are also treated as untrusted input: fixed keys only.
            keys=PUBLIC_FIELDS|{'submission_id','phase','tls','storage','updated_at','live_config_changed'}
            return {k:v for k,v in value.items() if k in keys}

    def write_status(self,submission_id,phase,**fields):
        if phase not in PHASES or set(fields)-PUBLIC_FIELDS:raise ValueError('invalid_status_fields')
        if 'live_config_changed' in fields and (type(fields['live_config_changed']) is not bool
                or fields['live_config_changed'] is True and phase!='active'):
            raise ValueError('live_config_changed_requires_active')
        if 'model_ids' in fields and (not isinstance(fields['model_ids'],list) or len(fields['model_ids'])>500
                or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',x) for x in fields['model_ids'])):
            raise ValueError('invalid_model_ids')
        for key in ('selected_model','message_code'):
            if key in fields and not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',str(fields[key])):raise ValueError('invalid_status_value')
        with self.lock:
            status=self.public_status()
            if status.get('submission_id')!=submission_id:return False
            submission=self.read_submission()
            if submission and submission['api_key'] in json.dumps(fields,ensure_ascii=False):
                raise ValueError('status_contains_credentials')
            status.update(fields,phase=phase,updated_at=time.time())
            if phase!='active':status['live_config_changed']=False
            self._status=status
            if not self.memory_only:self._write('status.json',json.dumps(status,allow_nan=False).encode())
            return True
