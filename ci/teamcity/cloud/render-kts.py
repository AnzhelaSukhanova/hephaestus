"""Re-embed step-<n>-*.sh into HephaestusNightlyFuzzing.kts: ``python3 ci/teamcity/cloud/render-kts.py``.

Only the n-th ``scriptContent = \"\"\" ... \"\"\".trimIndent()`` block is replaced by
step n, with every bash ``$`` written as ``${'$'}``; the rest of the file is kept.
``ci/teamcity/tests/dry-run.sh`` checks that the two stay in sync.
"""
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
KTS = HERE / 'HephaestusNightlyFuzzing.kts'
BLOCK = re.compile(r'(scriptContent = """\n)(.*?)(\n\s*"""\.trimIndent\(\))', re.S)
INDENT = ' ' * 12


def embed(script: Path) -> str:
    text = script.read_text().rstrip('\n')
    if '"""' in text:
        sys.exit(f'{script.name}: contains """, which would end the Kotlin raw string')
    text = text.replace('$', "${'$'}")
    return '\n'.join(INDENT + line if line else '' for line in text.split('\n'))


def main() -> int:
    steps = sorted(HERE.glob('step-*-*.sh'), key=lambda path: int(path.name.split('-')[1]))
    source = KTS.read_text()
    blocks = BLOCK.findall(source)
    if len(blocks) != len(steps):
        sys.exit(f'{KTS.name} has {len(blocks)} script blocks but there are {len(steps)} steps')
    contents = iter(embed(step) for step in steps)
    KTS.write_text(BLOCK.sub(lambda match: match.group(1) + next(contents) + match.group(3), source))
    print(f'{KTS.name}: embedded {", ".join(step.name for step in steps)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
