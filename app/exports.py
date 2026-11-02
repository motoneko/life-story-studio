import io
import json
import zipfile
from docx import Document
from docx.shared import Pt, Inches


def docx_bytes(project, chapters, draft=False):
    doc = Document()
    normal = doc.styles['Normal']
    normal.font.name = '宋体'
    normal.font.size = Pt(12)
    doc.add_heading(project['title'], 0)
    doc.add_paragraph(f'讲述者：{project["name"]}')
    doc.add_paragraph('审阅稿 · 不用于印刷' if draft else '已确认书稿 · 供线下排版')
    doc.add_page_break()
    doc.add_heading('目录', 1)
    for i, chapter in enumerate(chapters, 1):
        doc.add_paragraph(f'{i}. {chapter["title"]}')
    def add_photos(chapter_id):
        for photo in project.get('_photos', []):
            if photo['chapter_id'] == chapter_id:
                doc.add_picture(photo['file'], width=Inches(min(5.0, photo['width']/150)))
                doc.add_paragraph(' · '.join(v for v in (photo['caption'],photo['taken_at'],photo['people']) if v))
    for i, chapter in enumerate(chapters, 1):
        doc.add_page_break()
        doc.add_heading(f'第 {i} 章　{chapter["title"]}', 1)
        for paragraph in chapter['body'].split('\n'):
            if paragraph.strip():
                doc.add_paragraph(paragraph)
        add_photos(chapter.get('id', ''))
        if draft:
            questions = json.loads(chapter['questions'])
            if questions:
                doc.add_heading('待核实事项', 2)
                for question in questions:
                    doc.add_paragraph(question)
    if any(not p['chapter_id'] for p in project.get('_photos', [])):
        doc.add_page_break()
        doc.add_heading('照片附录', 1)
        add_photos(None)
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def bundle(project, chapters, sources, audit, books, release_manifest=None):
    """Create a typesetting handoff, not a backup of private interview material.

    Keep the audit/books arguments for compatibility with existing callers; neither
    belongs in the handoff. In particular, ASR segments can contain words removed
    from the edited transcript and must never leak back into an exported package.
    """
    referenced_ids = set()
    for chapter in chapters:
        referenced_ids.update(json.loads(chapter['source_ids']))
    source_index = [
        {key: source[key] for key in ('id', 'name', 'kind')}
        for source in sources if source['id'] in referenced_ids
    ]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('book.docx', docx_bytes(project, chapters))
        z.writestr('book.md', '# ' + project['title'] + '\n\n' + '\n\n'.join('## ' + c['title'] + '\n\n' + c['body'] for c in chapters))
        for photo in project.get('_photos', []):
            z.write(photo['file'], 'photos/' + photo['id'] + '.jpg')
        if project.get('_photos'):
            z.writestr('photo-index.json', json.dumps([{k:p[k] for k in ('id','caption','taken_at','people','chapter_id','width','height')} for p in project['_photos']], ensure_ascii=False, indent=2))
        z.writestr('source-index.json', json.dumps(source_index, ensure_ascii=False, indent=2))
        if release_manifest is not None:
            allowed = (
                'book_id', 'book_title', 'version', 'content_sha256',
                'approved_at', 'approval_scope', 'intended_audience',
                'chapter_count', 'word_count',
            )
            manifest = {
                key: release_manifest[key] for key in allowed
                if key in release_manifest and isinstance(release_manifest[key], (str, int, bool))
            }
            z.writestr('release-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr('README.txt',
                   '此包仅供已确认书稿的线下排版交付。book.docx 与 book.md 为正文；'
                   'source-index.json 只记录正文引用素材的编号、标题与类型，用于回到受权限保护的项目追溯来源。'
                   '素材标题也会随包交付，请在导出前检查其中是否含不应交给排版方的信息。'
                   '本包不含逐字稿、原始识别分段、录音、访谈对话、账户邮箱、内部操作日志或历史稿件。'
                   '如有 release-manifest.json，可核对所确认的版本及交付范围。'
                   '交付确认不等同于公开发行授权，商业出版、印刷及销售需另外办理。'
                   '原始采访资料应留在项目中由有权限的人员核对，不应直接转交排版方。')
    return stream.getvalue()
