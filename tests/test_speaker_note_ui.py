"""Speaker naming patches generated notes without swapping the recording workspace."""

import re
from pathlib import Path

from tests.test_outline_ui import run_node


def test_rename_updates_generated_note_projection_and_preserves_workspace():
    source = (Path(__file__).parents[1] / "src/localplaud/api/static/js/workspace.js").read_text()
    functions = []
    for name in ("applySpeakerNoteProjection", "renameSpeaker", "renameSpeakers"):
        match = re.search(rf"  (?:async )?function {name}\(.*?\n  }}", source, re.DOTALL)
        assert match
        functions.append(match.group(0))
    run_node("\n".join(functions), r"""
const controller = new AbortController(), signal = controller.signal;
const cfg = {fileId:'owned-recording'};
const CSS = {escape:value=>value};
const tr = value=>value;
const state = {speakers:new Map(), refreshNoteOutline:()=>{state.outlineRefreshed=true;}};
const audio = {currentTime:125, paused:false};
const prose = {innerHTML:'<p>Speaker 1 will follow up.</p>'};
const stale = {hidden:true}, warning = {hidden:true};
const draft = {value:'My unsaved edit'};
const panel = {hidden:false, querySelector:selector=>{
  if(selector==='[data-generated-note-prose]')return prose;
  if(selector==='[data-note-stale]')return stale;
  throw new Error('Unexpected note selector '+selector);
}};
const document = {querySelector:selector=>{
  if(selector==='[data-speaker-note-warning]')return warning;
  if(selector==='[data-note-panel="sum-7"]')return panel;
  return null;
}, getElementById:id=>{throw new Error('Unexpected workspace replacement '+id);}, dispatchEvent:event=>renamedEvents.push(event)};
const renamedEvents=[];
const updated = [], histories=[];
const requests=[];
const updateSpeakerEverywhere = key=>updated.push(key);
const loadOutline = ()=>{};
const hideRepeatedNoteTitle = ()=>{};
const updateNamedRecordingTitle = ()=>{};
const refreshNoteHistory = (host, html)=>histories.push({host,html});
let response;
const fetch = async(url,options)=>{requests.push({url,options});return response;};
(async()=>{
  response={ok:true,json:async()=>({name:'Sky',note_projection:{file_id:'owned-recording',notes:[
    {id:7,content_html:'<p>Sky will follow up.</p>',stale:false,history_html:'<details>v2</details>'},
    {id:999,content_html:'off-page note'},
  ]}})};
  await renameSpeaker('SPEAKER_00','Sky');
  assert.equal(prose.innerHTML,'<p>Sky will follow up.</p>');
  assert.equal(stale.hidden,true);
  assert.equal(state.speakers.get('SPEAKER_00'),'Sky');
  assert.equal(state.outlineRefreshed,true);
  assert.equal(histories[0].host,panel);
  assert.equal(histories[0].html,'<details>v2</details>');
  assert.deepEqual(audio,{currentTime:125,paused:false});
  assert.equal(panel.hidden,false);
  assert.equal(draft.value,'My unsaved edit');

  applySpeakerNoteProjection({file_id:'another-recording',notes:[{id:7,content_html:'wrong'}]});
  assert.equal(prose.innerHTML,'<p>Sky will follow up.</p>');
  response={ok:false,json:async()=>({error:'Name not saved',note_projection:{notes:[{id:7,content_html:'wrong'}]}})};
  await assert.rejects(renameSpeaker('SPEAKER_00','Rejected'),/Name not saved/);
  assert.equal(prose.innerHTML,'<p>Sky will follow up.</p>');
  assert.equal(state.speakers.get('SPEAKER_00'),'Sky');

  applySpeakerNoteProjection({notes:[{id:7,content_html:'<p>Still readable</p>',stale:true}]}, {unresolved:1});
  assert.equal(warning.hidden,false);
  assert.equal(stale.hidden,false);
  assert.equal(prose.innerHTML,'<p>Still readable</p>');
  response={ok:true,json:async()=>({names:{SPEAKER_00:'Sky',SPEAKER_01:'Kai'},note_projection:{notes:[{id:7,content_html:'<p>Sky and Kai</p>',stale:false}]}})};
  const before=requests.length;
  await renameSpeakers({SPEAKER_00:'Sky',SPEAKER_01:'Kai'});
  assert.equal(renamedEvents.at(-1).type,'localplaud:speakers-renamed');
  assert.equal(requests.length,before+1);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body.get('names')),{SPEAKER_00:'Sky',SPEAKER_01:'Kai'});
  assert.equal(state.speakers.get('SPEAKER_01'),'Kai');
  assert.equal(prose.innerHTML,'<p>Sky and Kai</p>');
  assert.equal(warning.hidden,true);
  response={ok:false,json:async()=>({error:'Unknown speaker'})};
  await assert.rejects(renameSpeakers({bad:'Wrong'}),/Unknown speaker/);
  assert.equal(state.speakers.has('bad'),false);
  controller.abort();
  applySpeakerNoteProjection({notes:[{id:7,content_html:'late'}]});
  assert.equal(prose.innerHTML,'<p>Sky and Kai</p>');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_first_speaker_rename_reveals_history_without_replacing_controller_node():
    from jinja2 import Environment, FileSystemLoader

    repo = Path(__file__).parents[1]
    environment = Environment(loader=FileSystemLoader(repo / "src/localplaud/api/templates"))
    environment.filters["markdown"] = str
    initial = environment.get_template("_note_history.html").render(history_summary={"versions": []})
    assert '<details class="transcript-tool note-history-tool" hidden>' in initial
    source = (repo / "src/localplaud/api/static/js/workspace.js").read_text()
    function = re.search(r"  function refreshNoteHistory\(.*?\n  }", source, re.DOTALL).group(0)
    run_node(function, r"""
const oldNode={hidden:true,children:[],replaceChildren(...children){this.children=children;},
  querySelector:()=>null,querySelectorAll:()=>[]};
const fresh={childNodes:[{version:1},{version:2}]};
const template={content:{querySelector:()=>fresh}};
const document={createElement:()=>template};
const host={querySelector:()=>oldNode};
const on=()=>{};
refreshNoteHistory(host,'<details>First archived history</details>');
assert.equal(host.querySelector(),oldNode);
assert.equal(oldNode.hidden,false);
assert.deepEqual(oldNode.children,fresh.childNodes);
""")


def test_recording_title_refresh_preserves_manual_draft_and_current_player():
    source = (Path(__file__).parents[1] / "src/localplaud/api/static/js/workspace.js").read_text()
    function = re.search(r"  function updateNamedRecordingTitle\(.*?\n  }", source, re.DOTALL).group(0)
    run_node(function, r"""
const cfg={fileId:'recording',title:'Speaker 1 meeting'};
const CSS={escape:value=>value};
const titles=[{}, {}, {}, {}];
const input={value:'Speaker 1 meeting',defaultValue:'Speaker 1 meeting'};
const form={hidden:true};
const document={title:'Speaker 1 meeting — localplaud',querySelectorAll:selector=>{
  assert.match(selector,/data-recording-id="recording"/);return titles;
},getElementById:id=>id==='recording-title'?input:form};
const playerUpdates=[];
const window={lp:{player:{updateTitle:(id,title)=>playerUpdates.push({id,title})}}};
updateNamedRecordingTitle({recording_title_changed:false,display_title:'Leave unchanged'});
assert.equal(cfg.title,'Speaker 1 meeting');
updateNamedRecordingTitle({recording_title_changed:true,display_title:'Sky meeting'});
assert.equal(cfg.title,'Sky meeting');
assert.equal(document.title,'Sky meeting — localplaud');
assert(titles.every(node=>node.textContent==='Sky meeting' && node.title==='Sky meeting'));
assert.equal(input.value,'Sky meeting');
assert.equal(input.defaultValue,'Sky meeting');
assert.deepEqual(playerUpdates,[{id:'recording',title:'Sky meeting'}]);
form.hidden=false;input.value='My unsaved custom title';
updateNamedRecordingTitle({recording_title_changed:true,display_title:'Kai meeting'});
assert.equal(input.value,'My unsaved custom title');
assert.equal(input.defaultValue,'Kai meeting');
""")


def test_mindmap_renders_speaker_headings_and_preserves_bullet_hierarchy():
    source = (Path(__file__).parents[1] / "src/localplaud/api/static/js/workspace.js").read_text()
    parser = re.search(r"  function parseMindMap\(.*?\n  }", source, re.DOTALL).group(0)
    renderer = re.search(r"    const render = \(node, depth = 0\) => {.*?\n    };", source, re.DOTALL).group(0)
    run_node(parser + "\n" + renderer, r"""
const tr=value=>value;
const speakerVars=['--sp0','--sp1'];
const document={createElement:()=>({children:[],style:{setProperty(){}},setAttribute(){},
  append(...nodes){this.children.push(...nodes);}})};
const parsed=parseMindMap('# Release\n## 小天\n- Review release\n## Riley\n- Confirm results');
assert.deepEqual(parsed,{text:'Release',children:[
  {text:'小天',children:[{text:'Review release',children:[]}]},
  {text:'Riley',children:[{text:'Confirm results',children:[]}]},
]});
const rendered=render(parsed),labels=[];
function visit(node){if(node.className==='mm-label')labels.push(node.textContent);node.children.forEach(visit);}
visit(rendered);
assert.deepEqual(labels,['Release','小天','Review release','Riley','Confirm results']);
const nested=parseMindMap('# Meeting\n## People\n### Sky\n- Task\n  - Evidence\n### Riley\n- Follow-up\n## Decisions\n- Ship');
assert.equal(nested.children[0].children[0].text,'Sky');
assert.equal(nested.children[0].children[0].children[0].children[0].text,'Evidence');
assert.equal(nested.children[0].children[1].text,'Riley');
assert.equal(nested.children[1].text,'Decisions');
assert.deepEqual(parseMindMap('# Bullets\n- First\n  - Child\n- Second'),{
  text:'Bullets',children:[{text:'First',children:[{text:'Child',children:[]}]},{text:'Second',children:[]}],
});
assert.equal(parseMindMap('# Release\n## Speaker 1\n- Review').children[0].text,'Speaker 1');
const escaped=parseMindMap(String.raw`# A\|B release
## A\|B
- \*Sky\*
- Path\\name
- Keep\q`);
assert.equal(escaped.text,'A|B release');
assert.equal(escaped.children[0].text,'A|B');
assert.deepEqual(escaped.children[0].children.map(node=>node.text),['*Sky*',String.raw`Path\name`,String.raw`Keep\q`]);
labels.length=0;
visit(render(escaped));
assert.deepEqual(labels,['A|B release','A|B','*Sky*',String.raw`Path\name`,String.raw`Keep\q`]);
""")
