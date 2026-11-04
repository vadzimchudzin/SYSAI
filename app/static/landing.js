/* Landing: the two-voice track under the wordmark and the record-to-tasks scheme. */
(() => {
  const css = getComputedStyle(document.documentElement);
  const RED = css.getPropertyValue('--clay').trim(), BLUE = css.getPropertyValue('--sblue').trim(), TXT = css.getPropertyValue('--text').trim();
  const ease = x => x<0?0:x>1?1:1-Math.pow(1-x,3);
  const t0 = performance.now();

  /* дорожка под надписью: красный и синий = два голоса */
  const cv = document.getElementById('voice'), cx = cv.getContext('2d');
  let W,H,N,bars=[];
  function size(){ const d=Math.min(devicePixelRatio||1,2); W=cv.clientWidth; H=cv.clientHeight; cv.width=W*d; cv.height=H*d; cx.setTransform(d,0,0,d,0,0);
    N=Math.floor(W/6); bars=[]; let a=11; const r=()=>{a=(a*16807)%2147483647;return a/2147483647};
    for(let i=0;i<N;i++){ const x=i/N, s=(x<.3||(x>.55&&x<.78))?0:1, gap=(Math.abs(x-.3)<.02||Math.abs(x-.55)<.02||Math.abs(x-.78)<.02);
      bars.push({s, a: gap?.05:(.3+.7*Math.pow(r(),.8))*(Math.sin(i*.7+r()*2)>-.5?1:.2), p:r()*6.28, v:1.4+r()*2}); } }
  function voice(t){ cx.clearRect(0,0,W,H); const mid=H/2, st=W/N, bw=Math.max(2,st*.5);
    const play = t<1.6?0:((t-1.6)*.06)%1;
    bars.forEach((b,i)=>{ const g=ease((t-.2-i*.006)/.5); if(g<=0) return;
      const h=Math.max(1.2,b.a*(.6+.4*Math.sin(t*b.v+b.p))*g*(mid-2));
      cx.globalAlpha = t<1.6 ? 1 : (i/N<play?1:.35); cx.fillStyle = b.s?BLUE:RED;
      cx.beginPath(); cx.roundRect(i*st+(st-bw)/2, mid-h, bw, h*2, bw/2); cx.fill(); });
    cx.globalAlpha=1; if(t>=1.6){ cx.fillStyle=TXT; cx.fillRect(play*W,0,1.5,H); } }

  /* схема */
  const NS='http://www.w3.org/2000/svg', flows=document.getElementById('flows');
  const wires=[0,1,2,3,4,5].map(i=>document.getElementById('w'+i)), colors=[TXT,RED,RED,BLUE,BLUE,RED];
  const tracks=wires.map((w,i)=>{ const len=w.getTotalLength();
    const tr=document.createElementNS(NS,'path'); tr.setAttribute('d',w.getAttribute('d')); tr.setAttribute('class','trail'); tr.style.stroke=colors[i]; tr.style.strokeDasharray=len; tr.style.strokeDashoffset=len; flows.appendChild(tr);
    const halo=document.createElementNS(NS,'circle'); halo.setAttribute('r',11); halo.style.fill=colors[i]; halo.style.opacity=0; flows.appendChild(halo);
    const dot=document.createElementNS(NS,'circle'); dot.setAttribute('r',5); dot.style.fill=colors[i]; dot.style.opacity=0; flows.appendChild(dot);
    return {w,len,tr,dot,halo}; });
  const dsts=[...document.querySelectorAll('.dst')], core=document.getElementById('core'), step=document.getElementById('step'), cap=document.getElementById('cap'), clock=document.getElementById('clock');
  const dots=[...core.querySelectorAll('.dots i')];
  const barsEl=document.getElementById('bars'); for(let i=0;i<34;i++){ const e=document.createElement('i'); if((i>7&&i<15)||(i>22&&i<29)) e.className='b'; barsEl.appendChild(e); }
  const be=[...barsEl.children];
  const CYCLE=9, IN=[.4,1.4], ST=[[1.4,'расшифровка по голосам'],[2.05,'протокол и решения'],[2.7,'задачи и сроки']], OUT=3.3, GAP=.2, DUR=.95;
  const sm = x => x<0?0:x>1?1:(x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2);
  function track(k,p,f){ const o=tracks[k]; o.tr.style.strokeDashoffset=o.len*(1-p); o.tr.style.opacity=f;
    if(p>0&&p<1){ const pt=o.w.getPointAtLength(o.len*p); for(const c of [o.dot,o.halo]){c.setAttribute('cx',pt.x);c.setAttribute('cy',pt.y);} o.dot.style.opacity=1; o.halo.style.opacity=.2; }
    else { o.dot.style.opacity=0; o.halo.style.opacity=0; } }
  function say(el, text){ if (el.dataset.want === text) return; el.dataset.want = text; el.style.opacity = 0;
    clearTimeout(el._t); el._t = setTimeout(() => { el.textContent = text; el.style.opacity = 1; }, 220); }
  function board(T){ const t=T%CYCLE, live=t<IN[1]+.2?1:.25, f=t>CYCLE-1?Math.max(0,CYCLE-t):1;
    be.forEach((e,i)=>{ e.style.transform='scaleY('+((18+70*Math.abs(Math.sin(T*(2+(i*37%10)/4)+i*1.7))*live*(.55+.45*Math.sin(i*.9)))/100).toFixed(3)+')'; });
    track(0,sm((t-IN[0])/(IN[1]-IN[0])),f);
    let k=-1; ST.forEach((s,i)=>{ if(t>=s[0]) k=i; });
    core.classList.toggle('busy', t>=IN[1]&&t<OUT);
    say(step, t<IN[1]?'принимает запись':(k>=0&&t<OUT?ST[k][1]:'раздаёт задачи'));
    dots.forEach((d,i)=>d.classList.toggle('on', t>=IN[1]&&i<=k));
    for(let i=1;i<=5;i++){ const p=sm((t-OUT-(i-1)*GAP)/DUR); track(i,p,f); dsts[i-1].classList.toggle('hit', p>=1&&t<CYCLE-.6); }
    say(cap, t<IN[1]?'запись получена':t<OUT?'обработка':t<OUT+5*GAP+DUR?'рассылка':'готово · 5 получателей');
    clock.textContent = '00:0'+Math.min(9,Math.floor(t)); }

  function frame(now){ const t=(now-t0)/1000; voice(t); board(t); requestAnimationFrame(frame); }
  addEventListener('resize',size); size(); requestAnimationFrame(frame);
})();
