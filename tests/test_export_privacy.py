"""The typesetter must receive the approved manuscript, not the interview archive."""
import io
import json
import zipfile

from app.exports import bundle


def handoff(release_manifest=None):
    return bundle(
        {'title': '江边的岁月', 'name': '讲述者'},
        [{
            'title': '童年', 'body': '我在江边长大。',
            'source_ids': json.dumps(['used-source']),
            'questions': json.dumps(['SECRET_INTERNAL_QUESTION']),
        }],
        [{
            'id': 'used-source', 'name': '第一次访谈', 'kind': 'audio',
            'text': 'SECRET_EDITED_TRANSCRIPT',
            'segments': [{'text': 'SECRET_REDACTED_ORIGINAL'}],
            'path': 'SECRET_PRIVATE_AUDIO_PATH',
        }, {
            'id': 'unused-source', 'name': 'SECRET_UNUSED_SOURCE', 'kind': 'text',
            'text': 'SECRET_UNUSED_TRANSCRIPT',
        }],
        [{'actor': 'SECRET_EDITOR_EMAIL@example.com', 'action': 'SECRET_INTERNAL_AUDIT'}],
        [{'body': 'SECRET_HISTORICAL_MANUSCRIPT'}],
        release_manifest=release_manifest,
    )


def test_handoff_omits_interview_text_and_redacted_original():
    with zipfile.ZipFile(io.BytesIO(handoff())) as archive:
        assert set(archive.namelist()) == {
            'book.docx', 'book.md', 'source-index.json', 'README.txt',
        }
        assert json.loads(archive.read('source-index.json')) == [{
            'id': 'used-source', 'name': '第一次访谈', 'kind': 'audio',
        }]
        assert '我在江边长大。' in archive.read('book.md').decode()
        for filename in archive.namelist():
            content = archive.read(filename)
            if filename.endswith('.docx'):
                with zipfile.ZipFile(io.BytesIO(content)) as document:
                    for part in document.namelist():
                        assert b'SECRET_' not in document.read(part)
            else:
                assert b'SECRET_' not in content


def test_handoff_manifest_is_minimal_and_drops_private_metadata():
    manifest = {
        'book_id': 'book-1', 'book_title': '江边的岁月', 'version': 2,
        'content_sha256': 'a' * 64, 'approved_at': '2026-11-01T00:00:00Z',
        'approval_scope': '供家族内部阅读及排版',
        'intended_audience': 'family', 'chapter_count': 1, 'word_count': 8,
        'actor': 'SECRET_PRIVATE_EMAIL', 'consent': 'SECRET_ORIGINAL_SIGNATURE',
        'audit': [{'text': 'SECRET_INTERNAL_AUDIT'}],
    }
    with zipfile.ZipFile(io.BytesIO(handoff(manifest))) as archive:
        exported = json.loads(archive.read('release-manifest.json'))
        assert exported['content_sha256'] == manifest['content_sha256']
        assert exported['chapter_count'] == 1
        assert 'actor' not in exported
        assert 'consent' not in exported
        assert 'audit' not in exported
        assert b'SECRET_' not in archive.read('release-manifest.json')


def test_handoff_manifest_does_not_serialize_nested_private_data():
    with zipfile.ZipFile(io.BytesIO(handoff({'book_id': {'private': 'SECRET_PAYLOAD'}}))) as archive:
        assert json.loads(archive.read('release-manifest.json')) == {}
