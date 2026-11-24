"""Pure DSH reply normalization: formulas go to the math renderer, chat stays text."""
from __future__ import annotations

import copy
import re

from hakimi.tools.math_render import prepare_text, _bare_math_spans


def _plain(*values):
    """Join distinct ordinary chat fragments without repeating the same bubble text."""
    output = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        key = re.sub(r'\s+', '', value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value.strip())
    return '\n'.join(output)


def _markdown(base, extra):
    """Append a formula block unless it already appears as (part of) the markdown."""
    base = base.strip() if isinstance(base, str) else ''
    extra = extra.strip() if isinstance(extra, str) else ''
    if not extra:
        return base
    if not base:
        return extra
    left = re.sub(r'\s+', '', base)
    right = re.sub(r'\s+', '', extra)
    boundary = r"A-Za-z0-9_{}()\\^+*/=<>.\-\u0370-\u03ff\u2070-\u209f"
    def contains(haystack, needle):
        return bool(re.search('(?<!['+boundary+'])'+re.escape(needle)+'(?!['+boundary+'])', haystack))
    if right == left or contains(left, right):
        return base
    if contains(right, left):
        return extra
    return base + '\n\n' + extra


_GREEK = dict(zip('αβγδεζηθικλμνξοπρστυφχψω',
                 ('alpha','beta','gamma','delta','epsilon','zeta','eta','theta','iota','kappa','lambda','mu','nu','xi','o','pi','rho','sigma','tau','upsilon','phi','chi','psi','omega')))
_GREEK.update(dict(zip('ΓΔΘΛΞΠΣΥΦΨΩ',('Gamma','Delta','Theta','Lambda','Xi','Pi','Sigma','Upsilon','Phi','Psi','Omega'))))
_SUB = str.maketrans('₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎','0123456789+-=()')
_SUP = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾','0123456789+-=()')
_SYMBOL = {'−':'-', '×':r'\times ', '·':r'\cdot ', '÷':r'\div ',
           '≤':r'\leq ', '≥':r'\geq ', '≠':r'\ne ', '≈':r'\approx ',
           '∝':r'\propto ', '∑':r'\sum ', '∫':r'\int ', '∂':r'\partial ',
           '∇':r'\nabla ', 'ℏ':r'\hbar ', '∞':r'\infty '}

def _format_bare(markdown):
    """Lossless notation substitutions only; never calculate or infer a formula."""
    opaque = re.compile(r'```.*?```|~~~.*?~~~|`[^`\n]+`|https?://[^\s<>]+|\[CQ:[^\]]*\]|(?m:^(?: {4}|\t)[^\n]*(?:\n|$))|(?<!\\)\$\$.*?\$\$|(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$|\\\[.*?\\\]|\\\(.*?\\\)', re.S)
    def convert(chunk):
        output=[];cursor=0
        for start,end in _bare_math_spans(chunk):
            raw=chunk[start:end]
            tex=re.sub(r'[₀-₉₊₋₌₍₎]+', lambda m:'_{'+m[0].translate(_SUB)+'}',raw)
            tex=re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾]+',lambda m:'^{'+m[0].translate(_SUP)+'}',tex)
            tex=''.join(('o' if ch=='ο' else '\\'+_GREEK[ch]+' ') if ch in _GREEK
                        else _SYMBOL.get(ch,ch) for ch in tex)
            # Unknown symbols stay literally visible; never guess their meaning.
            formatted='$'+tex+'$' if tex.isascii() else raw
            output.extend((chunk[cursor:start],formatted));cursor=end
        output.append(chunk[cursor:]);return ''.join(output)
    output=[];cursor=0
    for match in opaque.finditer(markdown):
        output.extend((convert(markdown[cursor:match.start()]),match[0]));cursor=match.end()
    output.append(convert(markdown[cursor:]));return ''.join(output)


def normalize_reply(result):
    """Return a reply whose formulas are all in math.markdown.

    Input and output use the existing fallback reply shape, so old math={...}
    replies and dsh_tools math.send/reply.send terminal results keep working.
    This function is pure: it does not mutate the input, call a model or render.
    """
    if not isinstance(result, dict):
        return result
    normalized = copy.deepcopy(result)
    if normalized.get('action') != 'reply':
        return normalized

    original_spec = normalized.get('math')
    spec = copy.deepcopy(original_spec) if isinstance(original_spec, dict) else {}
    def split(value, plain_side):
        value = value if isinstance(value, str) else ''
        parts = prepare_text(value, bare=True)
        if parts:
            return parts['before'], parts['markdown'], parts['after']
        return (value, '', '') if plain_side == 'before' else ('', '', value)
    text_before, text_math, text_after = split(normalized.get('text'), 'before')
    before_before, before_math, before_after = split(spec.get('before'), 'before')
    after_before, after_math, after_after = split(spec.get('after'), 'after')
    if not spec and not text_math:
        return normalized
    # Keep the original logical order: text / before / body / after.
    markdown = ''
    for block in (text_math, before_math, spec.get('markdown', ''), after_math):
        markdown = _markdown(markdown, block)
    before = _plain(text_before, before_before)
    after = _plain(text_after, before_after, after_before, after_after)
    spec['markdown'] = _format_bare(markdown.strip())
    for key, value in (('before', before), ('after', after)):
        if value.strip():spec[key] = value.strip()
        else:spec.pop(key, None)
    normalized['math'] = spec
    normalized['text'] = ''
    return normalized
