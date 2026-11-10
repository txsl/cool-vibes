const $=id=>document.getElementById(id);
const dark=matchMedia('(prefers-color-scheme: dark)');
const isDark=()=>{const t=document.documentElement.dataset.theme;return t?t==='dark':dark.matches};
const cssVar=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// The server API. A preview build swaps in window.AIRCON_API with canned data.
const API=window.AIRCON_API||{
  async status(){const r=await fetch('/api/status');return r.json()},
  async history(hours){const r=await fetch('/api/history?hours='+hours);return r.json()},
  async post(path,body){
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok)throw new Error((await r.text())||r.statusText);
    return r.json();
  }
};

const MODE_NAME={0:'Fan',1:'Dry',2:'Auto',3:'Cool',4:'Heat',5:'Vent'};
const FAN_NAME={0:'Auto',1:'Low',2:'Low-Med',3:'Medium',4:'Med-High',5:'High'};
const MODE_COLOR={3:['#0a6cdc','#5ab0ff'],4:['#d4520c','#ff9d5c'],2:['#7038d6','#b994ff'],0:['#4a5568','#c3ccd8'],1:['#08806f','#4fd8c2'],5:['#4a5568','#c3ccd8']};

// The controller reports whole degrees, so the raw room series flickers between
// neighbours. Charts draw a 15-minute centred average (rs); tooltips show the raw value.
function parseHistory(j){
  const d=j.t.map((ts,i)=>({t:ts*1000,room:j.room[i],sp:j.sp[i],power:j.power[i],mode:j.mode[i],fan:j.fan[i]})).filter(x=>x.room!=null);
  const half=7.5*60*1000;let a=0,b=0,sum=0;
  for(let i=0;i<d.length;i++){
    while(b<d.length&&d[b].t<=d[i].t+half){sum+=d[b].room;b++}
    while(d[a].t<d[i].t-half){sum-=d[a].room;a++}
    d[i].rs=sum/(b-a);
  }
  return d;
}
// Consecutive samples that share a state, as [tStart, tEnd, sample] runs, split at data gaps.
function runs(data,t0,key){
  const out=[];let cur=null;
  for(let i=1;i<data.length;i++){const a=data[i-1],b=data[i];if(b.t<t0)continue;
    if(b.t-a.t>=GAP){cur=null;continue}
    const k=key(a);if(k==null){cur=null;continue}
    if(cur&&cur.k===k&&cur.end===a.t)cur.end=b.t;else{cur={k,start:Math.max(a.t,t0),end:b.t,s:a};out.push(cur)}}
  return out;
}

const GAP=10*60*1000;   // a hole longer than this (server down) breaks the lines
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const hhmm=d=>`${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;

// Room temperature (solid) and target (dashed, only while running) over time,
// with off periods shaded. `compact` is the homepage version: fewer labels,
// no mode strip. Returns the geometry so a caller can map pointer x to a sample.
function drawChart(cv,data,opt={}){
  if(opt.empty)opt.empty.style.display=data.length<2?'block':'none';
  cv.style.display=data.length<2?'none':'block';
  if(data.length<2)return null;
  const dpr=devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;
  cv.width=W*dpr;cv.height=H*dpr;
  const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);
  const C={room:cssVar('--chart-room'),target:cssVar('--chart-target'),off:cssVar('--chart-off'),grid:cssVar('--chart-grid'),label:cssVar('--ink3')};
  const strip=opt.compact?0:8;
  const pad=opt.compact?{l:30,r:4,t:6,b:20}:{l:36,r:8,t:10,b:36+strip+6};
  const x0=pad.l,x1=W-pad.r,y0=pad.t,y1=H-pad.b;
  const t1=opt.end||data[data.length-1].t,t0=opt.span?t1-opt.span:data[0].t;
  let lo=Infinity,hi=-Infinity;
  for(const d of data){if(d.t<t0)continue;lo=Math.min(lo,d.room);hi=Math.max(hi,d.room);if(d.power&&d.sp!=null){lo=Math.min(lo,d.sp);hi=Math.max(hi,d.sp)}}
  lo=Math.floor(lo-1);hi=Math.ceil(hi+1);if(hi-lo<4){const m=(hi+lo)/2;lo=Math.floor(m-2);hi=Math.ceil(m+2)}
  const X=t=>x0+(t-t0)/(t1-t0)*(x1-x0),Y=v=>y1-(v-lo)/(hi-lo)*(y1-y0);

  // running periods, tinted by mode
  const di=isDark()?1:0,running=runs(data,t0,d=>d.power?d.mode:null);
  for(const r of running){g.fillStyle=(MODE_COLOR[r.k]||MODE_COLOR[0])[di]+(di?'2e':'1f');const xa=X(r.start);g.fillRect(xa,y0,X(r.end)-xa,y1-y0)}

  // grid + y labels
  g.font='500 11px -apple-system,system-ui,sans-serif';g.lineWidth=1;g.textBaseline='middle';
  const yStep=(hi-lo)<=6?1:(hi-lo)<=12?2:5;
  for(let v=Math.ceil(lo/yStep)*yStep;v<=hi;v+=yStep){const y=Math.round(Y(v))+.5;
    g.strokeStyle=C.grid;g.beginPath();g.moveTo(x0,y);g.lineTo(x1,y);g.stroke();
    g.fillStyle=C.label;g.textAlign='right';g.fillText(v+'°',x0-6,y)}

  // x ticks on clock-aligned local times
  const spanMin=(t1-t0)/60000;
  const stepMin=opt.compact?(spanMin<=360?60:360):spanMin<=90?15:spanMin<=180?30:spanMin<=540?60:spanMin<=1080?180:spanMin<=2160?360:spanMin<=4800?720:1440;
  const start=new Date(t0);start.setHours(0,0,0,0);g.textAlign='center';g.textBaseline='alphabetic';let prevDay=null;
  for(let k=0;;k++){const td=new Date(start);td.setMinutes(k*stepMin);const tk=td.getTime();if(tk>t1)break;if(tk<t0)continue;
    const x=Math.round(X(tk))+.5;g.strokeStyle=C.grid;g.beginPath();g.moveTo(x,y0);g.lineTo(x,y1);g.stroke();
    const tx=Math.min(Math.max(x,x0+16),x1-16);g.fillStyle=C.label;
    g.fillText(stepMin>=1440?`${td.getDate()} ${MON[td.getMonth()]}`:hhmm(td),tx,y1+14);
    if(!opt.compact&&stepMin<1440&&td.getDate()!==prevDay)g.fillText(`${td.getDate()} ${MON[td.getMonth()]}`,tx,y1+27);
    prevDay=td.getDate()}

  // mode strip: what the unit was doing, coloured by mode, empty while off
  if(strip){const sy=H-strip-2;
    g.fillStyle=C.off;g.beginPath();g.roundRect(x0,sy,x1-x0,strip,3);g.fill();
    for(const r of running){g.fillStyle=(MODE_COLOR[r.k]||MODE_COLOR[0])[di];const xa=X(r.start);g.fillRect(xa,sy,Math.max(X(r.end)-xa,1.5),strip)}}

  // target, stepped, only while running
  g.save();g.strokeStyle=C.target;g.lineWidth=1.6;g.setLineDash([4,3]);g.beginPath();let on=false;
  for(let i=0;i<data.length;i++){const d=data[i],p=data[i-1];if(d.t<t0)continue;
    const run=d.power&&d.sp!=null&&!(p&&d.t-p.t>=GAP);
    if(run){const x=X(d.t),y=Y(d.sp);if(!on||!p||!p.power||p.sp==null){g.moveTo(x,y)}else{g.lineTo(x,Y(p.sp));g.lineTo(x,y)}on=true}else on=false}
  g.stroke();g.restore();

  // room temperature, with a soft fill underneath; each unbroken stretch is its own shape
  const segs=[];let seg=null;
  for(let i=0;i<data.length;i++){const d=data[i],p=data[i-1];if(d.t<t0)continue;
    if(!seg||(p&&d.t-p.t>=GAP)){seg=[];segs.push(seg)}seg.push([X(d.t),Y(d.rs)])}
  const grad=g.createLinearGradient(0,y0,0,y1);grad.addColorStop(0,C.room+'38');grad.addColorStop(1,C.room+'00');
  g.strokeStyle=C.room;g.lineWidth=2.2;g.lineJoin='round';
  for(const sg of segs){if(sg.length<2)continue;
    const line=new Path2D();sg.forEach(([x,y],i)=>i?line.lineTo(x,y):line.moveTo(x,y));
    const area=new Path2D(line);area.lineTo(sg[sg.length-1][0],y1);area.lineTo(sg[0][0],y1);area.closePath();
    g.fillStyle=grad;g.fill(area);g.stroke(line)}

  // hover crosshair
  if(opt.hover!=null){const d=data[opt.hover],x=X(d.t);
    g.strokeStyle=C.label;g.lineWidth=1;g.beginPath();g.moveTo(x+.5,y0);g.lineTo(x+.5,y1);g.stroke();
    const dot=(v,c)=>{g.fillStyle=c;g.beginPath();g.arc(x,Y(v),4,0,7);g.fill();g.strokeStyle=cssVar('--card')||'#fff';g.lineWidth=2;g.stroke()};
    if(d.power&&d.sp!=null)dot(d.sp,C.target);dot(d.rs,C.room)}
  return {X,t0,t1,x0,x1};
}
