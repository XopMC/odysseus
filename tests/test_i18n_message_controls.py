"""Real message/OCR/theme constructors localize controls, never their payloads."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_message_and_theme_controls_in_real_browser(tmp_path):
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('playwright')"], capture_output=True).returncode:
        pytest.skip('Node/Playwright is unavailable')
    repo = Path(__file__).resolve().parents[1]
    script = r"""
      const {chromium}=require('playwright'),fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
      const repo=process.argv[1],out=process.argv[2],calls=[],errors=[];
      const html=fs.readFileSync(repo+'/static/index.html','utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
      const server=http.createServer(async(req,res)=>{
        const path=new URL(req.url,'http://localhost').pathname;
        if(path.startsWith('/static/')){res.setHeader('Content-Type',path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'application/octet-stream');res.end(fs.readFileSync(repo+path));return;}
        if(path.startsWith('/api/')){calls.push({path,method:req.method});res.setHeader('Content-Type','application/json');res.end(JSON.stringify(path==='/api/prefs/ui_language'?{value:'ru'}:path.endsWith('/vision')?{text:'Save Delete — OCR original'}:{}));return;}
        res.setHeader('Content-Type','text/html');res.end(html);
      });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
      const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
      try{
        const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(String(error)));
        await page.goto('http://127.0.0.1:'+server.address().port);
        await page.evaluate(async()=>{
          document.getElementById('app-loader')?.remove();
          window.locale=await import('/static/js/i18n.js');locale.applyUiLanguage('ru');
          window.renderer=await import('/static/js/chatRenderer.js');locale.applyUiLanguage('ru');window.editCalls=0;
          window.chatModule={editMessage:()=>{window.editCalls++;}};
          const history=document.getElementById('chat-history');history.innerHTML='';
          const msg=document.createElement('div');msg.id='qa-message';msg.className='msg msg-ai';msg.dataset.raw='Save Delete original';msg.innerHTML='<div class="body">Save Delete original</div>';history.append(msg);msg.append(renderer.createMsgFooter(msg));
          const image=renderer.buildImageBubble('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/jFkAAAAASUVORK5CYII=','Copy prompt','Save','1x1','standard','image-qa');image.id='qa-image';history.append(image);
          renderer.displayMetrics(msg,{response_time:2,input_tokens:80000,output_tokens:10,tokens_per_second:5,tps_source:'stream_elapsed',context_percent:80,context_length:100000,model:'Save',usage_source:'real'});
          const attachments=renderer.buildAttachCards([{id:'locale-ocr',name:'Save.png',mime:'image/png',previewUrl:image.querySelector('img').src}]);attachments.id='qa-attachments';history.append(attachments);
          renderer.addMessage('assistant','Save original interrupted text','Save',{stopped:true});
        });
        assert.equal(await page.locator('#qa-image .footer-copy-btn').first().getAttribute('title'),'Копировать запрос');
        assert.equal(await page.locator('#qa-image .footer-open-gallery-btn span').textContent(),'Открыть в галерее');
        assert.equal(await page.locator('#qa-image .generated-image-caption').textContent(),'Copy prompt');
        assert.equal(await page.locator('#qa-image .role').textContent(),'Save');
        assert.equal(await page.locator('.continue-btn').last().getAttribute('title'),'Продолжить');
        await page.locator('#qa-message .msg-more-btn').click();
        assert((await page.locator('.msg-overflow-item').allTextContents()).some(text=>text.trim()==='✂ Сделать короче'));
        assert.equal(await page.locator('.msg-overflow-item[title="Сделать короче"] .overflow-icon').count(),1);
        await page.keyboard.press('Escape');
        assert((await page.locator('#qa-message .response-metrics').textContent()).includes('≈5 tok/s'));
        await page.locator('#qa-message .response-metrics').click();
        assert.equal(await page.locator('.ctx-popup > div').first().textContent(),'Статистика сообщения');
        assert((await page.locator('.ctx-popup .ctx-label').allTextContents()).includes('Ввод'));
        assert((await page.locator('.ctx-popup .ctx-label').allTextContents()).includes('Источник скорости'));
        assert((await page.locator('.ctx-popup').innerText()).includes('Оценка по времени потока'));
        assert((await page.locator('.ctx-popup').innerText()).includes('Провайдер не передал скорость декодирования; это оценка.'));
        await page.keyboard.press('Escape');
        await page.evaluate(()=>renderer.displayMetrics(document.getElementById('qa-message'),{response_time:2,input_tokens:80000,output_tokens:10,tokens_per_second:131.98,tps_source:'backend',context_percent:80,context_length:100000,model:'Save',usage_source:'real'}));
        assert((await page.locator('#qa-message .response-metrics').textContent()).includes('131.98 tok/s'));
        assert.equal((await page.locator('#qa-message .response-metrics').textContent()).includes('≈'),false);
        await page.locator('#qa-message .response-metrics').click();
        assert((await page.locator('.ctx-popup').innerText()).includes('Данные backend'));
        await page.keyboard.press('Escape');await page.locator('#qa-message .ctx-ring').click();
        assert.equal(await page.locator('.ctx-compact-btn').textContent(),'Сжать контекст');
        assert.equal(await page.locator('.ctx-compact-btn').getAttribute('title'),'Сжать старые сообщения, чтобы освободить контекст');
        await page.keyboard.press('Escape');await page.locator('#qa-attachments .attach-ocr-btn').click();
        assert.equal(await page.locator('.vision-editor-title span').textContent(),'Текст распознавания');
        await page.waitForFunction(()=>document.querySelector('.vision-editor-text').value==='Save Delete — OCR original');
        assert.deepEqual(await page.locator('.vision-btn-label').allTextContents(),['Закрыть','Сохранить','Повторить ответ на сообщение']);
        await page.screenshot({path:out+'/locale-ocr.png',animations:'disabled'});
        await page.locator('.vision-editor-btn').first().click();
        await page.evaluate(async()=>{
          document.getElementById('theme-font-select').append(new Option('Save','custom-save'));
          await (await import('/static/js/theme.js')).initThemeUI();
          document.getElementById('theme-popup').parentElement.classList.remove('hidden');
        });
        await page.locator('#theme-tabs [data-tab="theme-tab-customize"]').click();
        assert.equal((await page.locator('#theme-popup-header h4').textContent()).trim(),'Тема');
        await page.locator('#theme-adv-toggle').click();
        assert.equal(await page.locator('#adv-userBubbleBg').evaluate(node=>node.previousElementSibling.textContent),'Сообщение пользователя');
        assert.equal(await page.locator('#harmony-accent').evaluate(node=>node.closest('.theme-fd-group').querySelector('label').textContent),'Акцентный цвет');
        assert.equal(await page.locator('[data-reset-adv="userBubbleBg"]').getAttribute('title'),'Сбросить этот цвет');
        await page.locator('#clr-bg').click();
        assert.deepEqual(await page.locator('.cp-section-label').allTextContents(),['Сочетания','Недавние']);
        const swatches=await page.locator('.cp-suggestions .cp-swatch').evaluateAll(nodes=>nodes.map(node=>({hex:node.dataset.hex,title:node.title})));
        assert.equal(swatches.length,5);assert.equal(swatches[0].title,'Дополнительный цвет: '+swatches[0].hex);
        await page.evaluate(()=>locale.applyUiLanguage('en'));
        assert((await page.locator('#theme-adv-toggle').textContent()).includes('More Colors'));
        assert.equal(await page.locator('[data-reset-adv="userBubbleBg"]').getAttribute('title'),'Reset this color');
        assert.equal(await page.locator('#theme-font-select option[value="mono"]').textContent(),'Monospace');
        assert.equal(await page.locator('#theme-font-select option[value="custom-save"]').textContent(),'Save','cloning must not discover user font names as UI labels');
        assert.equal(await page.locator('.cp-suggestions .cp-swatch').first().getAttribute('title'),'Complement: '+swatches[0].hex);
        assert.deepEqual(await page.locator('.cp-suggestions .cp-swatch').evaluateAll(nodes=>nodes.map(node=>node.dataset.hex)),swatches.map(row=>row.hex));
        assert.equal(await page.locator('#qa-image .footer-copy-btn').first().getAttribute('title'),'Copy prompt');
        assert.equal(await page.locator('#qa-message .body').textContent(),'Save Delete original');
        await page.evaluate(()=>locale.applyUiLanguage('ru'));
        await page.screenshot({path:out+'/locale-theme-desktop.png',animations:'disabled'});
        await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/locale-theme-mobile.png',animations:'disabled'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
        assert.deepEqual(calls.filter(call=>call.method!=='GET'),[],'viewing controls must not mutate files, models or chats');
        assert.deepEqual(errors,[]);console.log('PASS');
      }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
    """
    result = subprocess.run(['node', '-e', '(async()=>{' + script + '})().catch(error=>{console.error(error);process.exit(1);});', str(repo), str(tmp_path)], capture_output=True, text=True, timeout=65, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
