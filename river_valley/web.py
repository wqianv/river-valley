"""Minimal standard-library HTTP adapter. The simulation has no UI imports."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading

from .core import Action, World, canonical


PAGE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>河谷 River Valley</title><style>
:root{font-family:system-ui,sans-serif;color:#26392f;background:#f4f1e8}body{max-width:1100px;margin:auto;padding:24px}
h1{margin:0}header{display:flex;justify-content:space-between;align-items:center;gap:16px}small{color:#52675a}
section{background:white;border:1px solid #dadfd5;border-radius:12px;padding:16px;margin-top:16px}
button,select,input{font:inherit;padding:8px;border:1px solid #9caca0;border-radius:6px;background:white}
button{cursor:pointer;background:#275a45;color:white}button:disabled{opacity:.5;cursor:wait}label{display:inline-block;margin:5px}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}svg{width:100%;height:auto;background:#edf2e5;border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px;border-bottom:1px solid #ddd;font-size:13px}
#error{color:#9a291b;min-height:24px}#events{max-height:300px;overflow:auto;font:13px ui-monospace,monospace}
@media(max-width:650px){body{padding:12px}header{display:block}table{font-size:12px}.scroll{overflow:auto}}
</style><body><header><div><h1>河谷 <small>River Valley · v0.1</small></h1><p>同一条河，两座村庄。观测状态，介入条件，追溯变化。</p></div><strong id="time"></strong></header>
<section><div class="controls"><button data-days="1">推进一天</button><button data-days="30">推进 30 天</button><button data-days="360">推进一年</button><button id="save">保存快照</button></div><p id="stats"></p>
<svg id="map" viewBox="0 0 900 400" role="img" aria-label="上下游村庄、河流和居民位置"></svg><small>绿色：农户 · 蓝色：商人 · 橙色：照料者 · 红色：玩家。悬停标记可查看身份。</small></section>
<section><h2>玩家行动</h2><p>玩家属于 h00 家庭；行动每天一次，受所在地、库存及农时约束。交易为按公开报价买入粮食。</p>
<div class="controls"><label>行动 <select id="kind"><option value="produce">生产 / 播种与耕作</option><option value="move">移动</option><option value="trade">购买粮食</option><option value="transfer">转移粮食</option><option value="divert">调整上游截流</option></select></label>
<label>地点 <select id="place"><option>upstream</option><option>downstream</option></select></label><label>目标家庭 <select id="target"></select></label>
<label>数量 / 截流百分比 <input id="amount" type="number" value="10" min="0" max="10000" style="width:90px"></label><button id="act">执行行动</button></div>
<p id="player"></p><p id="error" role="status" aria-live="polite"></p></section>
<section><h2>外生实验条件</h2><p>降雨调整属于实验输入，会被记录；它不是玩家或 NPC 的能力。留空恢复场景气候。</p><label>每天降雨量 <input id="rain" type="number" min="0" max="1000" placeholder="场景默认" style="width:120px"></label><button id="climate">应用降雨</button></section>
<section><h2>家庭与土地</h2><div class="scroll"><table><thead><tr><th>家庭</th><th>村庄</th><th>土地</th><th>粮食</th><th>货币</th><th>在田作物</th><th>累计缺食人日</th></tr></thead><tbody id="homes"></tbody></table></div></section>
<section><h2>最近事件</h2><small>显示最近 100 条；完整事件与直接来源保存在服务输出目录的 JSONL 日志中。</small><div id="events"></div></section>
<script>
const token='__TOKEN__';const $=id=>document.getElementById(id);let busy=false;
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-River-Token':token},body:body?JSON.stringify(body):undefined});let data=await r.json();if(!r.ok)throw Error(data.error);return data}
async function run(path,body){if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);$('error').textContent='';try{await api(path,body);await refresh()}catch(e){$('error').textContent=e.message}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}}
async function refresh(){let d=await api('/api/state');let s=d.state,m=d.summary;$('time').textContent=`第 ${Math.floor(s.day/360)+1} 年 · 第 ${s.day%360+1} 天 · ${s.season}`;
$('stats').textContent=`居民 ${m.population} · 粮食 ${m.grain} · 累计缺食 ${m.hunger_days} 人日 · 今日河水 ${s.river.inflow} · 上游/下游分水 ${s.river.upstream}/${s.river.downstream} · 场景 ${s.scenario}`;
$('player').textContent=`所在地：${s.people.player.location}；自家粮食：${s.households.h00.grain}；货币：${s.households.h00.coins}；今日已行动：${s.people.player.last_action===s.day?'是':'否'}`;
let map='<path d="M 180 0 Q 230 140 430 190 T 700 400" stroke="#79b9d0" stroke-width="40" fill="none"/><path d="M 240 120 L 285 100" stroke="#8b6545" stroke-width="8"/><text x="130" y="35">河流 → 下游</text>';
for(let [i,v] of ['upstream','downstream'].entries()){let x=300+i*260,y=120+i*130;map+=`<rect x="${x-30}" y="${y-30}" width="205" height="140" rx="16" fill="#e0dcc4"/><text x="${x}" y="${y-40}">${i?'下游村':'上游村'}</text>`;let people=Object.entries(s.people).filter(([id,p])=>p.location===v);people.forEach(([id,p],n)=>{let cx=x+(n%10)*16,cy=y+Math.floor(n/10)*14,color={farmer:'#39845b',trader:'#3679a3',carer:'#bd8638',player:'#c23f38'}[p.role];map+=`<circle cx="${cx}" cy="${cy}" r="${id==='player'?6:4}" fill="${color}"><title>${id} · ${p.role} · ${p.household}</title></circle>`})}$('map').innerHTML=map;
$('homes').innerHTML=Object.entries(s.households).map(([id,h])=>`<tr><td>${id}</td><td>${h.village}</td><td>${h.land}</td><td>${h.grain}</td><td>${h.coins}</td><td>${(h.crop/1000).toFixed(1)}</td><td>${h.hunger}</td></tr>`).join('');
if(!$('target').options.length)$('target').innerHTML=Object.keys(s.households).filter(h=>h!=='h00').map(h=>`<option>${h}</option>`).join('');
$('events').replaceChildren();d.events.slice().reverse().forEach(e=>{let line=document.createElement('p'),v=e.details,names={produce:'耕作',move:'移动',trade:'购买',transfer:'转移',divert:'截流'},description={clock:'日期推进',genesis:'世界初始化',climate_input:`实验降雨输入：${v.rainfall??'恢复场景'}`,action:`${names[v.kind]||v.kind} ${v.target||''} 数量 ${v.amount??''} · ${v.reason||''}`,hydrology:`降雨 ${v.rainfall} · 上游分水 ${v.allocations?.upstream??''} / 下游 ${v.allocations?.downstream??''}`,agriculture_and_consumption:`收获 ${v.harvest} · 消费 ${v.consumed} · 未满足粮食 ${v.unmet_food}`}[e.kind];line.textContent=`#${e.id} 日${e.day} ${e.actor||''} ${description||e.kind}${e.cause===null?'':` · 来源事件 #${e.cause}`}`;$('events').append(line)})}
document.querySelectorAll('[data-days]').forEach(b=>b.onclick=()=>run('/api/advance',{days:Number(b.dataset.days)}));$('save').onclick=()=>run('/api/save',{});
$('act').onclick=()=>{let kind=$('kind').value;run('/api/action',{kind,target:kind==='move'?$('place').value:$('target').value,amount:Number($('amount').value)})};
$('climate').onclick=()=>run('/api/rainfall',{value:$('rain').value===''?null:Number($('rain').value)});refresh().catch(e=>$('error').textContent=e.message);
</script></body></html>'''


def make_server(world, host="127.0.0.1", port=8000, output="runs/interactive"):
    lock = threading.Lock()
    token = secrets.token_hex(24)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = (canonical(value) if content_type.startswith("application/json") else value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.respond(200, PAGE.replace("__TOKEN__", token), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                with lock:
                    payload = dict(state=world.snapshot(), summary=world.summary(),
                                   events=list(world.events)[-100:])
                self.respond(200, payload)
            else:
                self.respond(404, dict(error="not found"))

        def do_POST(self):
            if self.headers.get("X-River-Token") != token:
                self.respond(403, dict(error="invalid session token"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 10000:
                    raise ValueError("invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                with lock:
                    if self.path == "/api/advance":
                        days = data["days"]
                        if type(days) is not int or not 1 <= days <= 360:
                            raise ValueError("advance 1..360 days")
                        world.advance(days)
                    elif self.path == "/api/action":
                        world.act(Action("player", data["kind"], data.get("target"), data.get("amount", 0)))
                    elif self.path == "/api/rainfall":
                        world.set_rainfall(data["value"])
                    elif self.path != "/api/save":
                        self.respond(404, dict(error="not found"))
                        return
                    world.save(destination / "snapshot.json")
                    if world.event_sink is not None:
                        world.event_sink.flush()
                self.respond(200, dict(ok=True))
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, dict(error=str(error)))

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    # New sessions get independent files; resumed logs are segments, not a
    # pretend full history. Preserve the original full log for concatenation.
    suffix = secrets.token_hex(4)
    name = f"segment-{suffix}.jsonl" if args.load else f"events-{suffix}.jsonl"
    with (output / name).open("w", encoding="utf-8", buffering=1) as sink:
        world = World.load(args.load, sink) if args.load else World(args.seed, args.scenario, sink)
        server = make_server(world, args.host, args.port, output)
        print(f"River Valley listening on {args.host}:{server.server_port}; log: {output / name}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            world.save(output / "snapshot.json")
            server.server_close()
