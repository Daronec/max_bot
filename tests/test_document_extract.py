import bot_test_support
from io import BytesIO
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from assistant_app.knowledge.extract import extract


class PDFExtractTests(unittest.TestCase):
    def setUp(self):
        try:
            import pypdf
        except ImportError:self.skipTest('Optional pypdf dependency is not installed')
        self.pypdf=pypdf
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'document.pdf'

    def test_text_layer_and_page_markers(self):
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=self.pypdf.PdfWriter();page=writer.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 40 700 Td (Knowledge marker) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
        writer.write(self.path)
        text=extract(self.path)
        self.assertIn('[Страница 1]',text);self.assertIn('Knowledge marker',text)
        from assistant_app.core import server
        with patch.object(server, 'TASKS_DIR', str(self.path.parent/'tasks')), patch.object(server, 'active_tasks', {}):
            client=server.app.test_client()
            response=client.post('/api/knowledge', data={'file':(BytesIO(self.path.read_bytes()), 'Manual.PDF')})
            self.assertEqual(response.status_code, 200)
            source=client.post('/api/knowledge/search', json={'query':'Knowledge marker'}).json['results'][0]
            self.assertEqual(source['title'], 'Manual.PDF')
            self.assertIn('[Страница 1]', client.get(source['url']).json['document']['chunks'][0]['content'])
            self.assertEqual(client.post('/api/knowledge',data={'file':(BytesIO(b'fixture'), 'wrong.docx')}).status_code,400)

    def test_scans_and_encryption_report_explicit_error(self):
        writer=self.pypdf.PdfWriter();writer.add_blank_page(width=600,height=800);writer.write(self.path)
        with self.assertRaisesRegex(ValueError,'OCR'):extract(self.path)
        writer.encrypt('private');writer.write(self.path)
        with self.assertRaisesRegex(ValueError,'Зашифрованный'):extract(self.path)


if __name__=='__main__':unittest.main()
