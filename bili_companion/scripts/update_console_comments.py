"""Install the comment view in the existing unified console, preserving its other views."""
from pathlib import Path
import argparse
import hashlib
import os
import tempfile


VIEW = """ const comments = document.getElementById('comments');
 comments.replaceChildren();
 const attempts = [...(data.video_comment_attempts || []), ...(data.comment_reply_attempts || [])];
 attempts.sort((a, b) => b.at - a.at);
 for (const row of attempts) {
   const item = document.createElement('article');
   const confirmed = row.visibility === 'public_confirmed' || row.state === 'public_confirmed';
   const labels = {silent:'决定不回复', generating:'已登记回复意图', not_confirmed:'回查尚未确认',
     account_only_readable:'发布账号读到，匿名未读到', both_not_read:'发布账号与匿名均未读到',
     public_confirmed:'匿名详情已读到', check_blocked:'本次核验受阻'};
   const status = labels[row.read_status] || labels[row.visibility] ||
     (confirmed ? labels.public_confirmed : labels[row.state] || commentLabels[row.state] || '状态未知');
   const kind = row.parent ? '回复' : '顶层评论';
   item.append(link(row), element('p', kind+' · '+status+' · '+time(row.at),'meta'));
   if (row.incoming) item.append(element('div','对方：'+row.incoming,'meta'));
   if (row.text) item.append(element('div',row.text,'detail'));
   const receipt = [];
   if (row.notice) receipt.push(row.notice);
   if (row.code && row.code !== 'accepted') receipt.push('返回码 '+row.code);
   if (Number.isInteger(row.review_state)) receipt.push('提交时状态 '+row.review_state);
   if (Number.isInteger(row.account_review_state)) receipt.push('本人回查状态 '+row.account_review_state);
   if (row.read_checked_at) receipt.push('核验于 '+time(row.read_checked_at));
   if (row.anonymous_code) receipt.push('匿名查询 '+row.anonymous_code);
   if (row.account_code) receipt.push('登录查询 '+row.account_code);
   if (receipt.length) item.append(element('div',receipt.join(' · '),'meta'));
   if (/^BV[a-zA-Z0-9]{10}$/.test(row.bvid) && Number.isSafeInteger(row.rpid) && row.rpid > 0) {
     const locator = element('a','定位评论 '+row.rpid);
     locator.href = 'https://www.bilibili.com/video/'+row.bvid+'/#reply'+row.rpid;
     locator.target = '_blank';
     locator.rel = 'noopener noreferrer';
     item.append(locator);
   }
   comments.append(item);
 }
 if (!comments.children.length) comments.append(element('p','暂无评论尝试。','empty'));"""


def install(path):
    original = path.read_text(encoding='utf-8')
    if VIEW in original:
        return
    lines = original.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines)
               if line.startswith(" const comments=document.getElementById('comments');")
               and "data.video_comment_attempts" in line and "暂无评论尝试" in line]
    if len(matches) == 1:
        lines[matches[0]] = VIEW + '\n'
    else:
        start = original.find(VIEW.splitlines()[0])
        end_line = VIEW.splitlines()[-1]
        end = original.find(end_line, start) + len(end_line)
        previous = original[start:end]
        if hashlib.sha256(previous.encode()).hexdigest() not in {
                '19b1a1c6d30f817c8abf15859ca490233efaca0371d5d41dd635e7197044c59a',
                '65ea7f2ba81470422e8c3759fe9a5f6c3334c3434b5f34166e9ad7eed7841ff0'}:
            raise ValueError('Unrecognized activity view; update skipped')
        lines = [original[:start], VIEW, original[end:]]
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        output.write(''.join(lines).encode('utf-8'))
        temp = Path(output.name)
    temp.chmod(path.stat().st_mode)
    os.replace(temp, path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('activity_js', type=Path)
    install(parser.parse_args().activity_js)

