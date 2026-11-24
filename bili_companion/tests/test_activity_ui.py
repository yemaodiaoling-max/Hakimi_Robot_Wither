"""Exercise the actual inline activity renderer with a small DOM harness."""
import json
import shutil
import subprocess
import unittest
from bili_companion.panel.activity import ACTIVITY_PAGE


@unittest.skipUnless(shutil.which('node'), 'Node is required for renderer verification')
class ActivityUITests(unittest.TestCase):
    def test_join_chips_disclosure_and_budget_pacing(self):
        script=ACTIVITY_PAGE.split('<script nonce="NONCE">',1)[1].split('</script>',1)[0]
        script=script[:script.index("document.getElementById('refresh').addEventListener")]
        harness=r'''
const assert=require('node:assert/strict');
class E {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.events={};this._text='';this.open=false;}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 append(...children){for(const c of children){c.parentElement=this;this.children.push(c);}}
 replaceChildren(...children){this.children=[];this._text='';this.append(...children);}
 addEventListener(name,cb){this.events[name]=cb;}get hash(){return this.href.slice(this.href.indexOf('#'));}
 scrollIntoView(){this.scrolled=true;this.scrollCalls=(this.scrollCalls||0)+1;}focus(){this.focused=true;this.focusCalls=(this.focusCalls||0)+1;}
}
const roots={};for(const id of ['runtime','memory','context-details','context-library','videos','unwatched','pending','comments']){roots[id]=new E(id==='context-library'?'details':'div');roots[id].id=id;}
roots['context-library'].append(roots['context-details']);
function find(e,id){if(e.id===id)return e;for(const c of e.children){const v=find(c,id);if(v)return v;}}
global.document={querySelectorAll:()=>Object.values(roots).flatMap(all).filter(e=>e.className==='memory-entry'&&e.open),createElement:tag=>new E(tag),getElementById:id=>roots[id]||Object.values(roots).map(e=>find(e,id)).find(Boolean)};
global.location={hash:''};global.history={replaceState:(a,b,hash)=>{location.hash=hash;}};
'''
        checks=r'''
const ref={source_id:'qq:M7424',revision:2,state:'available',summary:'共享音乐摘记正文'};
render({runtime_status:{state:'VIDEO',understanding_state:'WAITING_VIDEO_BUDGET',next_observation_at:Date.now()/1000+60},qq_memory_reads:[{read_at:1,keyword:'音乐',status:'first_import',records:[ref]}],video_history:[{bvid:'BV1234567890',summary:'真实片段小结',status:'sampled',coverage:[],updated_at:4}],video_comment_attempts:[],video_selections:[{bvid:'BV1234567890',at:2,request_id:'old',reason:'旧理由',qq_context:[]},{bvid:'BV1234567890',at:3,request_id:'new',reason:'最新模型理由',qq_context:[ref]},{bvid:'BV0987654321',at:4,request_id:'pending',reason:'尚未观看',qq_context:[]}]});
assert.match(roots.videos.textContent,/最新模型理由/);assert.doesNotMatch(roots.videos.textContent,/旧理由|共享音乐摘记正文/);
assert.match(roots.videos.textContent,/QQ · M7424 r2/);assert.match(roots.unwatched.textContent,/尚未观看/);assert.doesNotMatch(roots.unwatched.textContent,/最新模型理由/);
assert.match(roots.runtime.textContent,/预算节奏等待/);assert.doesNotMatch(roots.runtime.textContent,/耗尽/);
const entry=document.getElementById(contextId(ref));assert.equal(entry.open,false);assert.equal(entry.parentElement.open,false);
function all(e){return [e,...e.children.flatMap(all)];}const a=all(roots.videos).find(e=>e.className==='chip');a.events.click({preventDefault(){}});
assert.equal(entry.open,true);assert.equal(entry.parentElement.open,true);assert.equal(entry.focused,true);assert.equal(entry.scrolled,true);
assert.equal(roots['context-library'].hidden,true);
const openIds=new Set(document.querySelectorAll('.memory-entry[open]').map(e=>e.id));
assert.ok(openIds.has(contextId(ref)));
// Refresh rebuilds the details, retaining disclosure without moving the page.
render({runtime_status:{},qq_memory_reads:[{read_at:1,keyword:'音乐',status:'first_import',records:[ref]}],video_history:[],video_comment_attempts:[],video_selections:[]});
const refreshed=document.getElementById(contextId(ref));assert.equal(refreshed.open,true);assert.equal(refreshed.parentElement.open,true);assert.equal(refreshed.scrollCalls,undefined);assert.equal(refreshed.focusCalls,undefined);
console.log('ok');
'''
        result=subprocess.run(['node','-'],input=harness+'\n'+script+'\n'+checks,text=True,encoding='utf-8',capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'ok')

