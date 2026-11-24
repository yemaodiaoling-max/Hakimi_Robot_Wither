"""Explicit same-provider native Gemini route; no fallback hosts or redirects."""
import json
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler,HTTPSHandler
from .gateway import CognitionUnavailable
from .provider_probe import _endpoint,_NoRedirect


class NativeGeminiProviderClient:
    count_kind='configured_upper_bound'

    def __init__(self,base_url,*,api_key_file,input_token_upper_bound,timeout=90):
        origin,path=_endpoint(base_url,False)
        if type(input_token_upper_bound) is not int or not 1<=input_token_upper_bound<=200000:
            raise ValueError('verified input token bound required')
        prefix=path[:-3] if path.endswith('/v1') else path[:-7] if path.endswith('/v1beta') else path
        self.base_url=origin+prefix+'/v1beta/models/'
        self.key_file=Path(api_key_file);self.input_upper=input_token_upper_bound;self.timeout=timeout
        self.last_http_status=None;self.last_error_features=();self.last_response_metadata={}

    def count_tokens(self,model,payload):
        self._validate(model,payload)
        return self.input_upper

    def _validate(self,model,payload):
        if type(model) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}',model):
            raise ValueError('invalid model ID')
        if payload.get('tools'):raise ValueError('provider tools forbidden')
        maximum=payload.get('generationConfig',{}).get('maxOutputTokens')
        if type(maximum) is not int or not 1<=maximum<=2048:raise ValueError('bounded output required')
        # This native route was rejected by the provider. Keep unsupported media
        # fail-closed instead of exposing an unbounded bypass around the verified
        # OpenAI MP4 envelope.
        texts=[]
        for content in [payload.get('systemInstruction',{}),*payload.get('contents',[])]:
            for part in content.get('parts',[]):
                if 'inlineData' in part or 'fileData' in part:raise CognitionUnavailable('native_media_not_verified')
                if type(part.get('text')) is str:texts.append(part['text'])
        size=sum(len(text.encode('utf-8')) for text in texts)
        if size>16000 or size+2048>self.input_upper:raise CognitionUnavailable('provider_input_bound_exceeded')
        if len(json.dumps(payload).encode())>24*1024*1024:raise ValueError('request too large')

    def generate(self,model,payload):
        self._validate(model,payload)
        key=self.key_file.read_text().strip()
        if not key or any(not 33<=ord(c)<=126 for c in key):raise ValueError('invalid key')
        opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context()),_NoRedirect())
        url=self.base_url+model+':generateContent'
        try:
            request=Request(url,data=json.dumps(payload,allow_nan=False).encode(),method='POST',
                headers={'Content-Type':'application/json','x-goog-api-key':key,'Authorization':'Bearer '+key})
            with opener.open(request,timeout=self.timeout) as response:
                if response.geturl()!=url:raise CognitionUnavailable('provider_redirect')
                raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise ValueError('response too large')
            result=json.loads(raw)
            version=result.get('modelVersion')
            self.last_response_metadata={'reported_model':version if type(version) is str and key not in version
                and re.fullmatch(r'[A-Za-z0-9._/-]{1,160}',version) else None,
                'reasoning_details_available':type(result.get('usageMetadata')) is dict and
                    'thoughtsTokenCount' in result['usageMetadata']}
            return result
        except HTTPError as exc:
            self.last_http_status=exc.code
            try:
                body=exc.read(65536).decode('utf-8','replace').lower()
                self.last_error_features=tuple(word for word in ('model','unknown','generatecontent','inlinedata',
                    'video','unsupported','format','thinkingconfig','thinkingbudget','thinkinglevel','mediaresolution') if word in body)
            except Exception:pass
            exc.close();raise CognitionUnavailable('provider_http_'+str(self.last_http_status)) from None
        except CognitionUnavailable:raise
        except Exception:raise CognitionUnavailable('provider_native_request_failed') from None
