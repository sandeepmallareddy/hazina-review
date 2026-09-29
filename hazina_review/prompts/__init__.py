"""The two instructions the model lanes send, kept as text files beside this module.

Each file is sent exactly as it stands, apart from its placeholders: `{history}` in both, for
the directory the history brief was written to, and `{n}` in the mining one, for the most
tasks it may list. `render` fills them in one pass, so a value that itself looks like a
placeholder is left as it came. Nothing else in a file is touched: the braces of the answer
shapes are written singly and stay that way.
"""

from __future__ import annotations

import re
from importlib import resources

_PLACEHOLDER = re.compile(r"\{(history|n)\}")


def template(name: str) -> str:
    """The file `name`.txt, unfilled."""
    return resources.files(__name__).joinpath(f"{name}.txt").read_text("utf-8")


def render(name: str, **values: object) -> str:
    """`name`.txt with each placeholder it holds replaced by `str()` of the value given for it.

    A placeholder with no value given is an error rather than a gap in the text."""
    return _PLACEHOLDER.sub(lambda found: str(values[found.group(1)]), template(name))
