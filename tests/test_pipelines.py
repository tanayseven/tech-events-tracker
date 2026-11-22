from tech_events.pipelines import _extraction_valid, _looks_like_blob


def test_blob_detection():
    assert _looks_like_blob('{\n  "@context": "https://schema.org"}')
    assert _looks_like_blob("{&quot;name&quot;:&quot;GopherCon&quot;}")
    assert not _looks_like_blob("Spain")
    assert not _looks_like_blob(None)


def test_extraction_invalid_when_field_is_json_blob():
    assert not _extraction_valid([{"date": '{"startDate": "2026-04-20"}', "country": "x"}])
    assert not _extraction_valid([{"date": "2026-04-20", "name": '{"@type": "Event"}'}])


def test_extraction_valid_with_plain_values():
    assert _extraction_valid([{"date": "2026-04-20", "country": "Spain"}])
    assert not _extraction_valid([{"date": None}])


def test_blob_detection_script_assignment():
    assert _looks_like_blob('window.emsEvent = {"name": "React Summit"}')
    assert not _looks_like_blob("Join us {in person} or online: details soon")
