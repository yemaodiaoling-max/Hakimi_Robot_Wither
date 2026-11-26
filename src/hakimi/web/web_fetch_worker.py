"""Disposable network worker; its parent can terminate DNS and slow redirects."""
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

from hakimi.core.http import NoRedirect

MAX_BYTES=2_000_000


def native(kind,args,timeout):
    from hakimi.models.dsh_text_judge import _installation,_provider
    from urllib.parse import urlsplit
    provider=_provider(_installation())
    if urlsplit(provider['base_url']).hostname!='api.deepseek.com':
        return {'error':'native_search_not_configured','billable':False}
    if kind=='fetch':
        from hakimi.web.public_pages import public_url
        try:args=dict(args,url=public_url(args['url']))
        except (ValueError,OSError):return {'error':'web_public_url_required','billable':False}
    request=Request('https://api.deepseek.com/v1/tools/'+kind,
                    data=json.dumps(args).encode(),headers={'Content-Type':'application/json',
                    'Authorization':'Bearer '+provider['api_key']})
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=timeout) as response:
            raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:return {'error':'web_response_too_large','billable':None}
        data=json.loads(raw)
        billable=bool(data.get('results')) if kind=='search' else bool(data.get('content'))
        return {'data':data,'billable':billable,'http_status':200}
    except HTTPError as exc:
        return {'error':'native_'+kind+'_http_error','http_status':exc.code,'billable':False}


def main():
    task=json.loads(sys.stdin.read(100000));timeout=max(.1,min(20,float(task['timeout'])))
    try:
        if task['kind'] in {'search','fetch'}:
            result=native(task['kind'],task['args'],timeout)
        elif task['kind']=='dsh_search':
            from hakimi.web.dsh_search import search
            result=search(task['args']['query'],task['args']['runtime'])
        elif task['kind']=='public':
            from hakimi.web.public_pages import fetch
            url,kind,body=fetch(task['args']['url'],timeout)
            result={'url':url,'kind':kind,'body':body,'billable':False}
        else:result={'error':'unsupported_web_operation','billable':False}
    except Exception as exc:
        result={'error':'web_request_failed','error_type':type(exc).__name__,'billable':None}
    sys.stdout.write(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
