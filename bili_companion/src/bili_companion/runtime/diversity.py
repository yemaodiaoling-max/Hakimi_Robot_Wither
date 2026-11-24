"""Selection hints only: never promote titles or categories to observed facts."""
import re


def topic(item):
    label = str(item.get('title', '')) + ' ' + str(item.get('category', ''))
    if re.search(r'炉石|hearthstone|酒馆战棋|瓦莉拉|铜须|卡雷|克苏恩|尤格萨隆|逗鱼时刻|神棍|巢母', label, re.I):
        return 'hearthstone'
    if item.get('category_id') in {17,171,172,65,173,121,136,19} or re.search(r'游戏|电竞|电子竞技|单机|网游|手游|音游|桌游|GMV|MUG', str(item.get('category', ''))):
        return 'gaming'
    outside_tids = {31,138,20,182,25,21,230,28}
    return 'other' if item.get('category_id') in outside_tids or item.get('category') in {'科学','Science'} else 'unknown'


def exploration_required(labels):
    labels = list(labels)[:8]
    return (len(labels) >= 2 and labels[0] == labels[1] and labels[0] not in {'unknown','other'}
            or labels.count('hearthstone') >= 4)


def candidates(recommendations, popular, seen, explore):
    result = []
    pool = [*recommendations, *popular]
    pool = [i for i in pool if i.get('bvid') and i['bvid'] not in seen]
    pool = list({i['bvid']: i for i in reversed(pool)}.values())[::-1]
    outside = [i for i in pool if topic(i) == 'other']
    ordered = outside[:6] + [i for i in pool if i not in outside[:6]][:len(outside[:6])]
    for item in ordered:
        if not item.get('bvid') or item['bvid'] in seen or any(x['bvid'] == item['bvid'] for x in result):
            continue
        hint = topic(item)
        # Unknown categories cannot prove an outside-gaming exploration candidate.
        if explore and (hint != 'other'):
            continue
        result.append(dict(item, selection_topic=hint, exploration_required=explore))
        if len(result) == 12:
            break
    return result
