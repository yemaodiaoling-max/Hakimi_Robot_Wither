"""TV anime premiere listings, retrieved from the Bangumi public catalogue."""
import json
import re
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from hakimi.web import web_quality

_CALENDAR = re.compile(r'新番|番表|当季动画|本季动画|在播动画|seasonal anime|anime (?:lineup|schedule)', re.I)


def calendar_results(query, public_request, today=None):
    if not _CALENDAR.search(query):
        return []
    today = today or datetime.now(ZoneInfo('Asia/Tokyo')).date()
    month_match = re.search(r'(?<!\d)(1[0-2]|0?[1-9])\s*月', query)
    year_match = re.search(r'(?<!\d)(20\d\d)(?!\d)', query)
    season = re.search(r'(冬|春|夏|秋)(?:季|番)', query)
    if not (month_match or season) and (year_match or re.search(r'上季|下季|去年|明年|上一季|下一季|next season|last season', query, re.I)):
        return []
    year = int(year_match[1]) if year_match else today.year
    month = int(month_match[1]) if month_match else ('冬春夏秋'.index(season[1]) * 3 + 1 if season else (today.month-1)//3*3+1)
    params = urlencode({'type':2, 'cat':1, 'year':year, 'month':month, 'sort':'rank', 'limit':20})
    response = public_request('https://api.bgm.tv/v0/subjects?' + params)
    try:
        items = json.loads(response.get('body','')).get('data')
    except (ValueError, AttributeError):
        return []
    if not isinstance(items,list):
        return []
    genres = [g for g in ('异世界','冒险','日常','恋爱','科幻','悬疑','运动') if g in query]
    ranked = []
    for item in items:
        if not isinstance(item,dict) or item.get('platform') != 'TV' or type(item.get('id')) is not int:
            continue
        try:
            aired = date.fromisoformat(item.get('date',''))
        except (TypeError, ValueError):
            continue
        if (aired.year,aired.month) != (year,month):
            continue
        title = item.get('name_cn') or item.get('name')
        if not isinstance(title,str) or not title.strip():
            continue
        tags = [str(t['name']) for t in (item.get('tags') or []) if isinstance(t,dict) and t.get('name')]
        summary = str(item.get('summary') or '').strip()[:550]
        detail = '标签：' + '、'.join(tags[:12]) + '。' + summary
        hits = sum(g in detail or g in title for g in genres)
        if genres and not hits:
            continue
        item_url = f"https://bgm.tv/subject/{item['id']}"
        info = f'Bangumi 电视动画《{title}》，条目所列首播日期 {aired.isoformat()}。' + detail
        source = web_quality.source({'url':item_url, 'title':title, 'content':info, 'provider':'bangumi_catalogue'}, read=True)
        if source:
            source['air_date'] = aired.isoformat()
            ranked.append((hits,source))
    ranked.sort(key=lambda pair:pair[0],reverse=True)
    return [source for _,source in ranked[:5]]
