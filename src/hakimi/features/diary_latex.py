"""Typeset archived diary entries with XeLaTeX; no third-party Python packages."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

_ESC = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '$': r'\$', '&': r'\&', '#': r'\#', '%': r'\%', '_': r'\_', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}

def _escape(value: str) -> str:
    result = []
    for ch in value:
        n = ord(ch)
        # Noto CJK covers supplementary CJK; most emoji do not have glyphs in it.
        if (0x1F000 <= n <= 0x1FAFF or 0x2600 <= n <= 0x27BF):
            result.append(r'\emoji{' + format(n, 'X') + '}')
        elif n in (0xFE0E, 0xFE0F, 0x200D):
            continue
        else:
            result.append(_ESC.get(ch, ch))
    return ''.join(result)

def _body(text: str) -> str:
    # Preserve line breaks, including blank lines, while allowing normal page breaks.
    lines = text.replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    return '\n'.join((r'\diaryline{' + _escape(line) + '}') if line else r'\diaryblank' for line in lines)

def _body_with_image(text: str, image_tex: str) -> str:
    if not image_tex:
        return _body(text)
    # Choose a paragraph boundary near the middle; for a single paragraph,
    # prefer an existing line break, then a sentence boundary in long prose.
    normalized = text.replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n')
    boundaries = [m.end() for m in re.finditer(r'\n\s*\n', normalized)]
    if not boundaries:
        boundaries = [m.end() for m in re.finditer(r'\n', normalized) if m.end() < len(normalized)]
    if not boundaries and len(normalized) >= 100:
        boundaries = [m.end() for m in re.finditer(r'[。！？!?；;]', normalized) if m.end() < len(normalized)]
    if not boundaries:
        return _body(normalized) + '\n' + image_tex
    split = min(boundaries, key=lambda p: abs(p - len(normalized) / 2))
    # For short multiline poems, keep the remaining stanzas together on a
    # fresh page rather than leaving a few orphaned lines on the final page.
    lines = [line for line in normalized.split('\n') if line.strip()]
    if len(boundaries) >= 2 and 15 <= len(lines) <= 17:
        split = boundaries[0]
    page_break = r'\clearpage' if len(boundaries) >= 2 and 15 <= len(lines) <= 24 else ''
    return _body(normalized[:split].rstrip('\n')) + '\n' + image_tex + '\n' + page_break + '\n' + _body(normalized[split:])

def make_tex(entries: list[dict], outdir: Path, title: str = '哈基米的日记', *, front_matter=True) -> Path:
    """Write a self-contained TeX source and copy referenced images to outdir.

    The caller controls entry ordering. Original input text is additionally saved verbatim
    as UTF-8 .txt files alongside the TeX, including emoji and line endings.
    """
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    pieces = []
    for i, entry in enumerate(entries, 1):
        day, heading, body = entry['day'], entry['title'], entry['text']
        if not all(isinstance(x, str) for x in (day, heading, body)):
            raise TypeError('day, title and text must be strings')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError('day must be YYYY-MM-DD')
        date.fromisoformat(day)
        (outdir / f'entry-{i:04d}-{day}.txt').write_bytes(body.encode('utf-8'))
        image_tex = ''
        if entry.get('image'):
            source = Path(entry['image'])
            if not source.is_absolute() or source.suffix.lower() not in ('.png', '.jpg', '.jpeg') or not source.is_file():
                raise ValueError('image must be an existing absolute PNG or JPEG path')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            image_name = f'image-{digest}{source.suffix.lower()}'
            target = outdir / image_name
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
            image_tex = r'\diaryimage{' + image_name + '}'
        spacing = r'\linespread{1.12}\selectfont' if len(body) >= 900 else ''
        pieces.append(r'\begingroup' + spacing + r'\chapter{' + _escape(heading) + r'}\noindent{\sffamily\small\color{inklight}' + day + r'}\quad{\color{accent}\rule[0.3ex]{10mm}{0.4pt}}\par\vspace{1em}' + '\n' + _body_with_image(body, image_tex) + r'\endgroup')
    preamble = r'''\documentclass[11pt,openany,oneside]{book}
\usepackage[paperwidth=155mm,paperheight=225mm,inner=21mm,outer=19mm,top=19mm,bottom=19mm,headheight=15pt]{geometry}
\usepackage{fontspec,xeCJK,graphicx,xcolor,fancyhdr,titlesec,hyperref,eso-pic}
\setmainfont{Noto Serif CJK SC}
\setsansfont{Noto Sans CJK SC}
\setCJKmainfont{Noto Serif CJK SC}
\setCJKsansfont{Noto Sans CJK SC}
\definecolor{paper}{HTML}{F8F3E9}
\definecolor{ink}{HTML}{302B27}
\definecolor{inklight}{HTML}{766A5D}
\definecolor{accent}{HTML}{9C8AA4}
\definecolor{papergrain}{HTML}{EDE5D8}
\pagecolor{paper}\color{ink}
% A few very faint, regularly spaced paper flecks, painted behind the page.
\newcommand{\grainrow}{\color{papergrain}\leaders\hbox{\rule{0.4pt}{0.4pt}\kern 24pt}\hfill\kern0pt}
\AddToShipoutPictureBG{\AtPageLowerLeft{\begin{picture}(0,0)
\put(0,70){\makebox(440,0){\grainrow}}\put(0,190){\makebox(440,0){\grainrow}}
\put(0,310){\makebox(440,0){\grainrow}}\put(0,430){\makebox(440,0){\grainrow}}
\put(0,550){\makebox(440,0){\grainrow}}\put(0,630){\makebox(440,0){\grainrow}}
\end{picture}}}
\hypersetup{unicode,hidelinks,pdfcreator={XeLaTeX}}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\linespread{1.25}
\widowpenalty=10000 \clubpenalty=10000 \displaywidowpenalty=10000
\emergencystretch=2em
\pagestyle{fancy}\fancyhf{}\fancyfoot[C]{\small\color{inklight}\thepage}
\renewcommand{\headrulewidth}{0pt}
\fancypagestyle{plain}{\fancyhf{}\fancyfoot[C]{\small\color{inklight}\thepage}\renewcommand{\headrulewidth}{0pt}}
\titleformat{\chapter}[display]{\sffamily\bfseries\Large}{}{0pt}{}
\titlespacing*{\chapter}{0pt}{0pt}{1em}
\newcommand{\diaryline}[1]{\noindent #1\par}
\newcommand{\diaryblank}{\vspace{0.7\baselineskip}}
\newcommand{\diaryimage}[1]{\par\vspace{0.7\baselineskip}\noindent\begin{minipage}{\linewidth}\centering\includegraphics[width=\linewidth,height=0.21\textheight,keepaspectratio]{#1}\end{minipage}\par\vspace{0.7\baselineskip}}
\IfFontExistsTF{Symbola}{\newfontfamily\emojifont{Symbola}}{\IfFontExistsTF{Noto Sans Symbols 2}{\newfontfamily\emojifont{Noto Sans Symbols 2}}{\newfontfamily\emojifont{Noto Sans CJK SC}}}
\newcommand{\emoji}[1]{{\emojifont\iffontchar\font "#1 \char"#1\else\sffamily\footnotesize [表情]\fi}}
\begin{document}
\begin{titlepage}\centering\vspace*{0.27\textheight}{\sffamily\bfseries\Huge ''' + _escape(title) + r'''\par}\vspace{1.5em}{\color{accent}\rule{30mm}{0.4pt}}\end{titlepage}
\renewcommand{\contentsname}{日记目录}
\frontmatter\tableofcontents\mainmatter
'''
    if not front_matter:
        preamble = preamble.split(r'\begin{titlepage}',1)[0] + '\n\\mainmatter\n'
    tex = outdir / 'diary.tex'
    tex.write_text(preamble + '\n\n'.join(pieces) + '\n\\end{document}\n', encoding='utf-8', newline='\n')
    return tex

def compile_pdf(entries: list[dict], outdir: Path, title: str = '哈基米的日记', *, front_matter=True) -> Path:
    """Generate TeX and compile exactly twice, with shell escape disabled."""
    tex = make_tex(entries, outdir, title, front_matter=front_matter)
    executable = shutil.which('xelatex')
    if not executable:
        raise RuntimeError('xelatex not found')
    for _ in range(2):
        result = subprocess.run([executable, '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error', tex.name], cwd=tex.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, check=False)
        if result.returncode:
            raise RuntimeError('xelatex failed:\n' + result.stdout.decode('utf-8', 'replace')[-5000:])
    pdf = tex.with_suffix('.pdf')
    if not pdf.is_file():
        raise RuntimeError('xelatex did not produce PDF')
    return pdf





