"""Commit a DSH fallback result through the guarded math/media/reply path."""
from hakimi.models.fallback_prompt import fallback_segments


def submit_fallback_result(result, claim, coordinator, base, *, finish=False):
    """Commit through the same guarded math/media path for tool and final replies."""
    if coordinator.check_token(claim['token'], 'dsh'):
        from hakimi.prompt.prompt_context import activate_context
        activate_context(claim['payload'], base, claim=claim)
    from hakimi.models.dsh_math import normalize_reply
    result=normalize_reply(result)
    if result.get('memory'):
        from hakimi.models.fallback_memory import save_proposal
        save_proposal(result,claim,coordinator,base)
    if claim.get('_reply_submitted'):
        return {'accepted': coordinator.complete(claim['token'], outcome='replied'),
                'reason': 'already_replied'}
    if result["action"] == "silent":
        accepted = coordinator.complete(claim["token"], outcome="no_reply")
    else:
        from hakimi.tools.math_render import prepare_text
        automatic_math=not result.get('math') and not result.get('diary_export') and prepare_text(result['text'])
        # Render before bubble splitting; | may be a math operator.
        extra_result=result
        if automatic_math:
            extra_result=dict(result,text='')
            from hakimi.tools.sticker_policy import evaluate_sticker
            if not evaluate_sticker(claim['payload']['session_key'],text=result['text'],base=base)['allowed']:
                extra_result.pop('sticker',None)
        segments = fallback_segments(extra_result, claim["payload"], base)
        if automatic_math:
            from hakimi.tools.math_render import submit_text
            submission=submit_text(coordinator,claim['token'],'dsh',result['text'],base,
                extra=segments,files=result.get('files'),reply_to=result.get('reply_to'),interaction=result.get('interaction'))
            accepted=submission['accepted']
            if not accepted and submission['reason']!='stale_owner':raise ValueError('automatic math submission failed')
        elif result.get('diary_export'):
            from hakimi.features.diary_store import submit_export
            submission=submit_export(coordinator,claim['token'],'dsh',result['diary_export'],base,segments)
            accepted=submission['accepted']
            if not accepted and submission['reason']!='stale_owner':raise ValueError('diary export failed')
        elif result.get('math'):
            from hakimi.tools.math_render import submit
            submission=submit(coordinator,claim['token'],'dsh',result['math'],base,
                                               prefix=segments,reply_to=result.get('reply_to'),files=result.get('files'),interaction=result.get('interaction'))
            accepted=submission['accepted']
            if not accepted and submission['reason']!='stale_owner':
                raise ValueError('invalid math submission')
        elif not segments and not result.get("files"):
            accepted = coordinator.complete(claim["token"], outcome="no_reply")
        else:
            submission = coordinator.submit_reply(claim["token"], segments, "dsh", files=result.get("files"), reply_to=result.get('reply_to'),interaction=result.get('interaction'))
            from hakimi.bridge.mention_segments import UNRESOLVED, CLARIFY
            if submission.get('reason') == UNRESOLVED:
                cleared = coordinator.select_mention(claim['token'], 'dsh', None)
                if cleared['accepted']:
                    submission = coordinator.submit_reply(claim['token'], [CLARIFY], 'dsh')
            accepted = submission["accepted"]
            if not accepted and submission["reason"] != "stale_owner":
                raise ValueError("invalid fallback submission")
    if accepted and result.get('action') != 'silent':
        claim['_reply_submitted'] = True
        if finish:
            coordinator.complete(claim['token'], outcome='replied')
    return submission if 'submission' in locals() else {'accepted': accepted, 'reason': 'completed'}
