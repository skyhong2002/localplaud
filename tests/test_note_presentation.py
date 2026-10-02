from localplaud.note_presentation import without_playback_citations


def test_remove_generated_playback_links_preserves_real_times_and_markdown():
    original = '## 討論\n\n- 下午 14:30 開會。 [00:12](/file/abc_123?t=12.5) [01:02:03](/file/abc_123?t=3723)\n- [文件](https://example.com)\n'
    assert without_playback_citations(original) == '## 討論\n\n- 下午 14:30 開會。\n- [文件](https://example.com)\n'


def test_no_changes_to_actual_time_labels_or_external_links():
    original = '[14:30] 開會；[00:10](https://example.com/video?t=10)'
    assert without_playback_citations(original) == original
