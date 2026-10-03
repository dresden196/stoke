"""Translation for what a person actually reads.

The engine's log (log(), emitter.log) mirrors what happened -- like Rufus's
own log, it stays in English so a report pasted into an issue means the same
thing to anyone reading it. This module is only for the two things a person
is meant to read and act on: the status phrase under the progress bar
(emitter.status(), Progress.phase()'s status argument) and UsbError messages.
Modules wrap those with _(); nothing else.

gettext.translation(), given languages=None, resolves the language itself
from LANGUAGE, then LC_ALL, then LC_MESSAGES, then LANG -- the same order as
the C library. pkexec starts the privileged engine with an empty environment,
so the window passes LANGUAGE (and LC_ALL/LANG) through explicitly on the
command line; see passThroughEnv() in stoke-qt's backend.cpp.
"""

import gettext
import os

DOMAIN = "stoke"
DEFAULT_LOCALE_DIR = "/usr/share/locale"


def locale_dir():
    """Overridable so a checkout or a test can point at its own po/mo tree."""
    return os.environ.get("STOKE_LOCALE_DIR", DEFAULT_LOCALE_DIR)


# fallback=True: with no catalog installed for the chosen language (or none
# chosen), every _() call just returns its argument unchanged -- English,
# same as before this module existed.
_translation = gettext.translation(DOMAIN, locale_dir(), fallback=True)
_ = _translation.gettext
