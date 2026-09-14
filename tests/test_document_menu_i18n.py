"""Execute authored document menu binding seams without loading editor content."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DocumentMenuTranslationTests(unittest.TestCase):
    def test_real_bindings_translate_action_labels_preserve_user_text_and_icons(self):
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const root=process.argv[1];
const {bindUiText,applyUiLanguage,t}=await import('file://'+root+'/static/js/i18n.js');
applyUiLanguage('ru',{querySelectorAll:()=>[]});
const node=text=>({textContent:text,childNodes:[{nodeType:1,tagName:'SVG'},{nodeType:3,nodeValue:text}],setAttribute(){},closest(){return null}});
const labels=['Save','Copy','Run','Download','Close','Delete'].map(node);
const title=node('Save'); // user-authored document title must stay English
const source=fs.readFileSync(root+'/static/js/document.js','utf8');
const start=source.indexOf('_docTabMenu.innerHTML = items;');
const end=source.indexOf("_docTabMenu.style.display = 'block';",start);
assert(start>=0&&end>start);
new Function('_docTabMenu','items','bindUiText',source.slice(start,end))({querySelectorAll:()=>labels},'',bindUiText);
labels.forEach((label,index)=>{assert.equal(label.childNodes[1].nodeValue,t(['Save','Copy','Run','Download','Close','Delete'][index],'ru'));assert.equal(label.childNodes[0].tagName,'SVG');});
labels.forEach(label=>assert.match(label.childNodes[1].nodeValue,/[А-Яа-я]/));
assert.equal(title.childNodes[1].nodeValue,'Save');
const library=fs.readFileSync(root+'/static/js/documentLibrary.js','utf8');
for(const control of ['openItem','cloneItem','exportItem','archiveItem','deleteItem']) assert(library.includes('bindUiText('+control+'.lastElementChild,'));
assert(library.includes('bindUiText(row.lastElementChild, item.label)'));
assert(source.includes("['doc-email-schedule-btn', 'Schedule Send...']"));
console.log('document menu translation seams passed');
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
