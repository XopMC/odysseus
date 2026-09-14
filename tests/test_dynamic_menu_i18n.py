"""Authored dynamic-menu seam regressions; never scan user content for translation."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DynamicMenuTests(unittest.TestCase):
    def test_settings_search_matches_translated_labels_without_admin_leak(self):
        script = r'''
import assert from 'node:assert/strict';
const root=process.argv[1],{SETTINGS_PANELS,searchSettingsPanels}=await import('file://'+root+'/static/js/settings/registry.js');
const {t}=await import('file://'+root+'/static/js/i18n.js');
for(const panel of SETTINGS_PANELS) {
 const label=t(panel.label,'ru');
 const visible=searchSettingsPanels(label,{isAdmin:true,translate:v=>t(v,'ru')});
 assert(visible.some(p=>p.id===panel.id),label);
 if(panel.adminOnly)assert(!searchSettingsPanels(label,{isAdmin:false,translate:v=>t(v,'ru')}).some(p=>p.id===panel.id));
}
assert(searchSettingsPanels('Внешний вид',{translate:v=>t(v,'ru')}).length>0);
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settings_transient_save_notices_use_current_language(self):
        script = r'''
import fs from 'node:fs';import assert from 'node:assert/strict';
const root=process.argv[1],source=fs.readFileSync(root+'/static/js/settings.js','utf8');
const {t,applyUiLanguage}=await import('file://'+root+'/static/js/i18n.js');
assert(!/\b(?:msg|ttsMsg|sttMsg)\.textContent = '(?:Saved|Failed to save)'/.test(source));
const statements=[...source.matchAll(/\b(msg|ttsMsg|sttMsg)\.textContent = t\('(Saved|Failed to save)'\)/g)];
assert(statements.length>=25);
for(const locale of ['ru','en']) {
 applyUiLanguage(locale,{querySelectorAll:()=>[]});
 for(const match of statements) {
  const node={textContent:''};new Function(match[1],'t',match[0])(node,t);
  assert.equal(node.textContent,t(match[2],locale));
  if(locale==='ru')assert.match(node.textContent,/[А-Яа-я]/);
  node.textContent='';applyUiLanguage(locale,{querySelectorAll:()=>[]});assert.equal(node.textContent,'');
 }
}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_team_static_explanations_use_translatable_nodes(self):
        script = r'''
import fs from 'node:fs';import assert from 'node:assert/strict';
const root=process.argv[1],source=fs.readFileSync(root+'/static/js/team-workspace.js','utf8');
const {t,hasUiTranslation}=await import('file://'+root+'/static/js/i18n.js');
for(const condition of ['hostEnabled','files.length']) {
 const match=source.match(new RegExp("uiElement\\('p', "+condition.replace('.','\\.')+" \\? '([^']+)' : '([^']+)'"));
 assert(match,'conditional explanation requires explicit language binding');
 for(const label of match.slice(1))assert.match(t(label,'ru'),/[А-Яа-я]/);
}
assert(source.includes("artifact.name ? element('strong', artifact.name) : uiElement('strong', 'Artifact')"),'user artifact names must not be translated');
const plain=[...source.matchAll(/(?<![\w])element\('(?:p|legend|h4)', '([^']+)'/g)].map(m=>m[1]);
assert.deepEqual(plain,[],'static Team explanations must use explicit translation bindings');
assert(!/setAttribute\('aria-label', '[^']+'\)/.test(source),'authored accessible names must switch language');
for(const match of source.matchAll(/bindUiText\([^,]+, '([^']+)', 'aria-label'\)/g)) {
 assert(hasUiTranslation(match[1]),match[1]);assert.match(t(match[1],'ru'),/[А-Яа-я]/);
}
for(const match of source.matchAll(/uiElement\('(?:p|legend|h4)', '([^']+)'/g)) {
 assert(hasUiTranslation(match[1]),match[1]);assert.match(t(match[1],'ru'),/[А-Яа-я]/);
}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_engineering_authored_controls_and_notices_have_russian_labels(self):
        script = r'''
import fs from 'node:fs';import assert from 'node:assert/strict';
const root=process.argv[1];const {hasUiTranslation,t}=await import('file://'+root+'/static/js/i18n.js');
const source=fs.readFileSync(root+'/static/js/engineering-workspace.js','utf8');
const labels=[...source.matchAll(/\b(?:button|field|say)\('([^'\n]+)'/g)].map(match=>match[1]);
labels.push(...[...source.matchAll(/\buiEl\('[^'\n]+',\s*'([^'\n]+)'/g)].map(match=>match[1]));
assert(labels.length>=133,'must inspect actual engineering constructors');
assert.deepEqual([...new Set(labels.filter(label=>!hasUiTranslation(label)))],[]);
for(const label of ['Search saved presets','Rename selected preset','Preset name','Saved context presets']) {
  assert(labels.includes(label)); assert.match(t(label,'ru'),/[А-Яа-я]/);assert.equal(t(label,'en'),label);
}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_final_menu_catalog_is_case_exact_and_recurrence_gender_neutral(self):
        script = r'''
import assert from 'node:assert/strict';
const {t}=await import('file://'+process.argv[1]+'/static/js/i18n.js');
for(const key of ['Translate','Archive section','Run now','Pause','Resume','History','Revert to default','Clear cache','Has attachments','Unread','Undone','Unanswered','Pending · 30d','Stale · >30d','Urgent','Reply soon','Action needed','Bills','Receipt','Travel','Spam','Square HD — 1024 × 1024','Widescreen — 1920 × 1080','Portrait — 1080 × 1920','Postcard — 1500 × 1050','Letter (300dpi) — 2550 × 3300','Select a size…'])assert.notEqual(t(key,'ru'),key,key);
assert.equal(t('Archive section','ru'),'Архив');
for(const [index,key] of ['1st','2nd','3rd','4th','5th'].entries())assert.equal(t(key,'ru'),'№'+(index+1));
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_menu_bindings_preserve_arrows_and_user_values(self):
        script = r'''
import fs from 'node:fs';import assert from 'node:assert/strict';
const root=process.argv[1];const {bindUiText,applyUiLanguage}=await import('file://'+root+'/static/js/i18n.js');
applyUiLanguage('ru',{querySelectorAll:()=>[]});
const node=text=>({childNodes:[{nodeType:3,nodeValue:text}],setAttribute(){},closest(){return null}});
for(const [file,line,variable,source] of [
 ['emailInbox.js','bindUiText(menuItem.children[1], a.label);','menuItem','Archive'],
 ['emailLibrary.js','bindUiText(item.children[1], a.label);','item','Delete'],
 ['gallery.js','bindUiText(it.lastElementChild, a.label);','it','Favorite'],
 ['calendar.js','bindUiText(it.lastElementChild, label);','it','Edit']]) {
 const text=fs.readFileSync(root+'/static/js/'+file,'utf8');assert(text.includes(line));
 const label=node(source),arrow=node('›'),icon={tagName:'SVG'},userValue=node('Favorite');
 const element={children:[icon,label,arrow],lastElementChild:label};
 new Function(variable,'a','label','bindUiText',line)(element,{label:source},source,bindUiText);
 assert.match(label.childNodes[0].nodeValue,/[А-Яа-я]/);assert.equal(arrow.childNodes[0].nodeValue,'›');
 assert.equal(userValue.childNodes[0].nodeValue,'Favorite');assert.equal(icon.tagName,'SVG');
}
const notes=fs.readFileSync(root+'/static/js/notes.js','utf8');
assert(notes.includes("'.note-reminder-menu-confirm > span'), 'Save'"));
assert(notes.includes('[data-note-ui]'));assert(notes.includes("['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']"));
assert(!notes.includes('querySelectorAll(\'input\').forEach'));
console.log('authored dynamic menu checks passed');
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_modules_parse(self):
        for name in ('gallery', 'notes', 'emailInbox', 'emailLibrary', 'calendar', 'tasks', 'documentLibrary'):
            result = subprocess.run(['node', '--check', str(ROOT / 'static/js' / (name + '.js'))], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_authored_suggestions_do_not_translate_contact_or_user_labels(self):
        script = r'''
import fs from 'node:fs';import assert from 'node:assert/strict';
const root=process.argv[1],source=fs.readFileSync(root+'/static/js/emailLibrary.js','utf8');
const begin=source.indexOf('function _renderSearchSuggestions(items) {');
const end=source.indexOf('\nfunction ',begin+1);assert(begin>=0&&end>begin);
const rows=[0,1,2].map(i=>({dataset:{idx:String(i)},children:[{},{}],addEventListener(){}}));
const menu={style:{},querySelectorAll:()=>rows};const bindings=[];
const items=[{kind:'filter',value:'filter:unread',label:'user-injected label'},
 {kind:'email',subject:'Unread'}, {kind:'contact',name:'Unread',email:'Unread@example.test'}];
new Function('document','_libSuggestionFocusIdx','_LIB_FILTER_OPTIONS','bindUiText','_acceptSuggestion',
 source.slice(begin,end)+';_renderSearchSuggestions('+JSON.stringify(items)+');')(
 {getElementById:()=>menu},-1,[{value:'filter:unread',label:'Unread'}],(node,label)=>bindings.push({node,label}),()=>{});
assert.equal(bindings.length,1);assert.equal(bindings[0].node,rows[0].children[1]);assert.equal(bindings[0].label,'Unread');
assert(menu.innerHTML.includes('Unread@example.test'));
const tasks=fs.readFileSync(root+'/static/js/tasks.js','utf8');assert(tasks.includes('bindUiText(item.icon ? btn.lastElementChild : btn, item.label)'));
const gallery=fs.readFileSync(root+'/static/js/gallery.js','utf8');assert(gallery.includes("'#gallery-editor-template option'"));
const library=fs.readFileSync(root+'/static/js/documentLibrary.js','utf8');assert(library.includes("archive: 'Archive section'"));
console.log('authored suggestions and final menu inventory passed');
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
