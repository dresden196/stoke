"""Translations for the window, domain `stoke-gtk`.

The English strings are the same ones the KDE window uses, placeholders
included: KDE's i18n() numbers them (%1, %2) where Python would write %s.
Keeping the KDE form means one set of catalogs can serve both windows, so
`fmt()` does the substitution here instead of the % operator.
"""

import gettext
import os

DOMAIN = "stoke-gtk"
DEFAULT_LOCALE_DIR = "/usr/share/locale"

_translation = gettext.translation(
    DOMAIN, os.environ.get("STOKE_GTK_LOCALE_DIR", DEFAULT_LOCALE_DIR), fallback=True)

_ = _translation.gettext
ngettext = _translation.ngettext
pgettext = _translation.pgettext


def N_(msgid):
    """Marks a string for extraction without translating it yet."""
    return msgid


def fmt(msgid, *args):
    """Fills %1, %2, ... the way KDE's i18n() does."""
    text = msgid
    for n, arg in reversed(list(enumerate(args, 1))):
        text = text.replace("%" + str(n), str(arg))
    return text
