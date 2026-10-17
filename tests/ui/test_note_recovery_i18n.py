"""Recovery UI must not fall back to English in the Traditional Chinese locale."""

import re
from pathlib import Path

from localplaud.i18n import catalog, translator

SCRIPTS = Path(__file__).resolve().parents[2] / "src/localplaud/api/static/js"


def test_recovery_and_rebuild_messages_reach_the_browser_catalog():
    editor = (SCRIPTS / "note-editor.js").read_text()
    recovery = editor.split("const preserveConflict =", 1)[1].split("const adoptCopy =", 1)[0]
    messages = set(re.findall(r"(?:tr|field|action)\('([^']+)'", recovery))
    statuses = re.search(r"tr\(existing \? '([^']+)' : '([^']+)'\)", recovery)
    assert statuses
    messages.update(statuses.groups())
    messages.update(re.findall(r"tr\('(Leave this page[^']+)'\)", editor))
    assert len(messages) == 17

    workspace = (SCRIPTS / "workspace.js").read_text()
    rebuild = workspace.split("let queued, result;", 1)[1].split("if (message) message.innerHTML", 1)[0]
    failures = set(re.findall(r"'(Speech settings saved\.[^']+)'", rebuild))
    assert len(failures) == 3

    browser_messages = catalog("zh-Hant-TW")
    translate = translator("zh-Hant-TW")
    for message in messages | failures:
        assert message in browser_messages, message
        translated = translate(message)
        assert translated == browser_messages[message]
        assert translated != message and re.search(r"[\u4e00-\u9fff]", translated), message
