"""Inspect a bounded local clip with the video companion's configured observer."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler, Request

from hakimi.tools.qq_video import approved, LIMIT
from hakimi.transport.official_base import NoRedirect

SAFE_MEDIA = ['-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,avi,mpeg,mpegts,flv']


def observe(source, companion, request_id, deadline):
    from bili_companion.runtime.qq_request import intervals, timeline_order, coverage_state
    meta = json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', *SAFE_MEDIA, '-show_entries',
         'format=duration:stream=codec_type', '-of', 'json', str(source)], timeout=8))
    duration = float(meta['format']['duration'])
    streams = {s['codec_type'] for s in meta['streams']}
    if 'video' not in streams or not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid_video')
    plan = intervals(duration) if duration >= 1 else [(0, duration)]
    order = timeline_order(len(plan))[:5]
    samples = []
    report = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(observe_sample, source, companion, request_id, index, plan[index], duration,
                            'audio' in streams, deadline) for index in order]
        for job in as_completed(jobs):
            try:
                samples.append(job.result())
            except Exception:
                continue
            samples.sort(key=lambda sample: sample['start'])
            coverage = coverage_state(duration, samples)
            report = dict(ok=True, source='qq_native_video_attachment', duration=duration, title='QQ视频附件',
                          observed_seconds=coverage['processed_seconds'],
                          complete=coverage['coverage_status']=='complete',
                          ending_seen=any(s['end'] >= duration - .001 for s in samples),
                          sampled_intervals=[{'start': s['start'], 'end': s['end']} for s in samples],
                          unfinished_samples=len(order)-len(samples), **coverage,
                          summaries=[dict(start=s['start'], end=s['end'], text=s['summary']) for s in samples],
                          overview='\n'.join(f"{s['start']:.1f}–{s['end']:.1f}秒：{s['summary']}" for s in samples),
                          evidence=[item for s in samples for item in s['evidence']],
                          missing_modalities=sorted({m for s in samples for m in s['missing_modalities']}),
                          limitations='画面为抽样观察。缺口内的内容尚未观看。')
            temp = source.parent / 'coverage.tmp'
            temp.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
            temp.replace(source.parent / 'coverage.json')
    if report is None:
        raise ValueError('no_grounded_observation')
    return report


def observe_sample(source, companion, request_id, index, interval, duration, has_audio, deadline):
    from bili_companion.domain import Scope
    from bili_companion.media.capture import MediaChunk
    start, length = interval
    end = min(duration, start+length)
    clip = source.parent / f'clip-{index}.mp4'
    remaining = deadline-time.monotonic()
    if remaining < 2:
        raise TimeoutError('viewing_deadline')
    try:
        subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', *SAFE_MEDIA, '-ss', str(start), '-i', str(source),
             '-t', str(length), '-map', '0:v:0', '-map', '0:a:0?',
             '-vf', 'fps=2,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-threads', '2',
             '-c:a', 'aac', '-ac', '1', '-b:a', '48k', str(clip)],
            check=True, timeout=min(25, remaining-.5))
        engine = companion.engine_factory()
        scope = Scope(engine.account_id, 'video', request_id)
        chunk = MediaChunk(clip, start, end, time.time(), 'mixed' if has_audio else 'video',
                           request_id, request_id, has_audio=has_audio, has_video=True)
        remaining = deadline-time.monotonic()
        if remaining <= 1:
            raise TimeoutError('viewing_deadline')
        timeout = min(90, remaining-.5)
        if hasattr(engine, 'set_call_timeout'):
            engine.set_call_timeout(timeout)
        elif hasattr(engine.client, 'timeout'):
            engine.client.timeout = timeout
        token = engine.ledger.owner.set(request_id)
        try:
            seen = engine.observe(chunk, scope, 0, allow_decision=False, video_context={
                'title': 'QQ视频附件', 'position': start, 'total_duration': duration,
                'continuous_viewing': True, 'comment_watch_eligible': False})
        finally:
            engine.ledger.owner.reset(token)
    finally:
        clip.unlink(missing_ok=True)
    evidence = [dict(id=item.id, text=item.text, media_start=item.media_start,
                     media_end=item.media_end, modality=item.modality, version=item.version)
                for item in seen.observation.evidence if item.scope == scope and item.session_id == request_id]
    if not evidence:
        raise ValueError('no_grounded_observation')
    return dict(start=start, end=end, summary=seen.summary or '；'.join(item['text'] for item in evidence),
                evidence=evidence, missing_modalities=seen.observation.missing_modalities)


def main():
    from bili_companion.cognition.qq_summary import normalize_feedback
    from bili_companion.runtime.qq_request import build
    task = json.load(sys.stdin)
    if not approved(task['url']):
        raise ValueError('unapproved_video_host')
    deadline = time.monotonic() + max(1, min(118, float(task['timeout'])))
    source = Path(task['tmp']) / 'source.bin'
    with build_opener(ProxyHandler({}), NoRedirect()).open(Request(task['url']), timeout=10) as res, source.open('wb') as out:
        while data := res.read(262144):
            if out.tell()+len(data) > LIMIT:
                raise ValueError('video_size_limit')
            if time.monotonic() >= deadline:
                raise TimeoutError('video_download_deadline')
            out.write(data)
    companion = build(json.loads(Path(task['config']).read_text(encoding='utf-8')))
    request_id = task['request_id']
    question = normalize_feedback(task.get('question'))
    report = task.get('observations')
    if report:
        report['observations_cached'] = True
    else:
        reserve = min(30, max(4, (deadline-time.monotonic())*.3)) if question else 0
        report = observe(source, companion, request_id, deadline-reserve)
    if question:
        companion.add_feedback(request_id, question, report, deadline, screenshot_path=source.parent/'frame.jpg',
                               source=source, input_options=SAFE_MEDIA)
    return report


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': 'video_attachment_' + type(exc).__name__}))
