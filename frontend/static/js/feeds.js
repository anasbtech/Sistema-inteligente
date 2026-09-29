/* ==========================================================
   SEGURANÇA — Motor de vídeo sintético (frontend)

   Renderiza cenas procedurais em <canvas> para dar imagem às
   câmeras enquanto não há RTSP real ligado. A troca por vídeo
   de verdade é local: no mosaico, substitua o <canvas> por um
   <video> apontado à URL WebRTC de GET /stream/{codigo}.

   Os dados (câmeras, detecções, pessoas) vêm todos da API —
   este arquivo só desenha.
   ========================================================== */
'use strict';

/* ---------- Utilidades ---------- */
function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function hash32(s){let h=2166136261;for(let i=0;i<String(s).length;i++){h^=String(s).charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
const rnd=()=>Math.random();
const rf=(a,b)=>Math.random()*(b-a)+a;
const ri=(a,b)=>Math.floor(Math.random()*(b-a+1))+a;
const pick=arr=>arr[Math.floor(Math.random()*arr.length)];

const CENAS={
  rua:       {lbl:'Via pública',      ceu:'#101C28', chao:'#15202B'},
  avenida:   {lbl:'Avenida',          ceu:'#0E1A26', chao:'#131E29'},
  praca:     {lbl:'Praça',            ceu:'#0F1D22', chao:'#16232A'},
  portaria:  {lbl:'Portaria',         ceu:'#141A22', chao:'#1A222B'},
  corredor:  {lbl:'Corredor interno', ceu:'#161D25', chao:'#1C242D'},
  estac:     {lbl:'Estacionamento',   ceu:'#0D141C', chao:'#141C25'},
  perimetro: {lbl:'Perímetro',        ceu:'#0B1219', chao:'#111A22'},
  terminal:  {lbl:'Terminal',         ceu:'#141C26', chao:'#1A232D'}
};

const SEXOS={
  M:{lbl:'Masculino',     pfx:'BIO-M', cor:'#35A7DE'},
  F:{lbl:'Feminino',      pfx:'BIO-F', cor:'#E05F9E'},
  N:{lbl:'Não informado', pfx:'BIO-N', cor:'#5F7589'}
};

const COR_CLASSE={
  pessoa:'#35A7DE', carro:'#2FB67C', caminhao:'#E8712C', onibus:'#D9C13A',
  motocicleta:'#F0A02A', bicicleta:'#8C6FE0', animal:'#4ED1C5',
  mochila:'#E05F9E', volume:'#E8474D', capacete:'#5FC4F0', placa:'#2FB67C', arma:'#E8474D',
  veiculo:'#2FB67C',   // vocabulário da cena sintética, que não distingue o tipo
  rosto:'#E05F9E',     // rosto e corpo saem de modelos diferentes sobre a mesma
                       // pessoa: cores distintas deixam ver as duas caixas
  FACIAL:'#E05F9E'
};
/* Só vale enquanto o worker não disser quanto dura a volta dele pelo parque:
   quem sabe disso é ele, e o número cresce a cada câmera cadastrada. Serve de
   piso para o primeiro quadro e para quando a mensagem vier sem a validade. */
const CAIXA_VIDA_MS=5000;
/* Quantos tiles podem manter fluxo MJPEG contínuo. O navegador permite seis
   conexões HTTP por origem, e o MJPEG não devolve a sua enquanto estiver
   tocando; o resto do sistema ainda precisa de vaga para as chamadas de API.
   Acima deste número os tiles passam a recarregar quadro a quadro. */
const MAX_MJPEG=4;
/* Cadência dos tiles que recarregam. A inferência leva mais que isso para
   voltar à mesma câmera, então o vídeo continua à frente do que o sistema
   consegue analisar. */
const POLL_MS=1000;


const SCENE_CFG={
  avenida :{hy:.30,lanes:4,veic:.85,pess:.5,  amb:'#0B1620'},
  rua     :{hy:.34,lanes:2,veic:.35,pess:.75, amb:'#0C1219'},
  praca   :{hy:.31,lanes:0,veic:.05,pess:1.0, amb:'#0C161A'},
  portaria:{hy:.36,lanes:1,veic:.18,pess:.65, amb:'#10161D'},
  corredor:{hy:.40,lanes:0,veic:0,  pess:.55, amb:'#12181F'},
  estac   :{hy:.29,lanes:0,veic:.55,pess:.4,  amb:'#0A1017'},
  perimetro:{hy:.33,lanes:0,veic:.08,pess:.3, amb:'#080E14'},
  terminal:{hy:.35,lanes:2,veic:.45,pess:1.1, amb:'#101820'}
};

class Scene{
  constructor(cam){
    this.cam=cam;
    this.cfg=SCENE_CFG[cam.cena]||SCENE_CFG.rua;
    this.rng=mulberry(hash32(cam.codigo));
    this.agents=[];
    this.t=rf(0,1000);
    this.props=this.buildProps();
    this.glitch=0;
    const n=Math.round(3+this.cfg.pess*4);
    for(let i=0;i<n;i++)this.spawn(true);
    if(this.cfg.veic>.2)for(let i=0;i<Math.round(this.cfg.veic*3);i++)this.spawn(true,'veiculo');
  }
  r(){return this.rng()}
  buildProps(){
    const p=[],r=()=>this.rng();
    if(this.cam.cena==='praca'){for(let i=0;i<7;i++)p.push({k:'arv',u:r()*2-1,d:.15+r()*.7,s:.7+r()*.6})}
    if(this.cam.cena==='perimetro'){for(let i=0;i<16;i++)p.push({k:'poste',u:-1.15+i*.15,d:.28+r()*.05})}
    if(this.cam.cena==='estac'){for(let i=0;i<8;i++)p.push({k:'parked',u:-.95+i*.27,d:.24+ (i%2)*.14 + r()*.05,c:pickCar(r())})}
    if(this.cam.cena==='terminal'){for(let i=0;i<5;i++)p.push({k:'col',u:-1+i*.5,d:.3+r()*.4})}
    if(this.cam.cena==='corredor'){for(let i=0;i<5;i++)p.push({k:'porta',u:(i%2?1:-1),d:.2+i*.15})}
    if(this.cam.cena==='rua'||this.cam.cena==='avenida'){for(let i=0;i<6;i++)p.push({k:'lum',u:(i%2?1:-1)*(.85+r()*.2),d:.18+i*.14})}
    if(this.cam.cena==='portaria'){p.push({k:'guarita',u:-.62,d:.42});p.push({k:'cancela',u:.35,d:.5})}
    return p;
  }
  spawn(inicial,forcar){
    const r=()=>this.rng();
    const tipo=forcar||(r()<this.cfg.veic/(this.cfg.veic+this.cfg.pess+.001)?'veiculo':'pessoa');
    const dir=r()>.5?1:-1;
    if(tipo==='veiculo'){
      const moto=r()<.22;
      this.agents.push({
        t:moto?'motocicleta':'veiculo', u:inicial?r()*1.8-.9:dir*-1.15,
        d:.24+r()*.5, vu:dir*(moto?.0042:.0028)*(.7+r()*.8), vd:(r()-.5)*.00012,
        cor:pickCar(r()), w:moto?.5:1, id:++Scene.SEQ, track:r()<.8, placa:r()<.5
      });
    }else{
      this.agents.push({
        t:'pessoa', u:inicial?r()*1.8-.9:dir*-1.1,
        d:.22+r()*.62, vu:dir*.0011*(.6+r()*.9), vd:(r()-.5)*.0004,
        cor:pickPed(r()), fase:r()*6.28, id:++Scene.SEQ, track:r()<.92,
        mochila:r()<.3, corre:false, parado:r()<.12, paradoAte:0
      });
    }
  }
  step(dt){
    this.t+=dt;
    if(this.glitch>0)this.glitch-=dt*2.4;
    if(this.rng()<.0016)this.glitch=1;
    for(let i=this.agents.length-1;i>=0;i--){
      const a=this.agents[i];
      if(a.parado){
        a.paradoAte-=dt;
        if(a.paradoAte<=0){a.parado=false}
      }else{
        a.u+=a.vu*dt*60*(a.corre?2.4:1);
        a.d+=a.vd*dt*60;
        if(a.d<.18)a.d=.18,a.vd=Math.abs(a.vd);
        if(a.d>.94)a.d=.94,a.vd=-Math.abs(a.vd);
      }
      if(a.t==='pessoa'&&!a.parado&&this.rng()<.0009){a.parado=true;a.paradoAte=1.5+this.rng()*4}
      if(a.t==='pessoa'&&this.rng()<.0006){a.corre=!a.corre}
      if(a.u<-1.25||a.u>1.25){this.agents.splice(i,1)}
    }
    const alvo=Math.round(3+this.cfg.pess*4+this.cfg.veic*2);
    if(this.agents.length<alvo&&this.rng()<.02)this.spawn(false);
    if(this.agents.length>alvo+5)this.agents.shift();
  }
  proj(u,d,W,H){
    const hy=this.cfg.hy*H;
    const k=Math.pow(d,1.55);
    const y=hy+(H-hy)*k;
    const sc=.07+.93*k;
    const x=W*.5+u*W*.62*(.3+.7*k);
    return {x,y,sc};
  }
}
Scene.SEQ=0;
function pickCar(r){const c=['#2C3E52','#4A5568','#6B4A3A','#3A5A4A','#5A3A46','#7A7F86','#1F2A38','#54606E'];return c[Math.floor(r*c.length)]}
function pickPed(r){const c=['#3E4C5C','#5A4A55','#46564C','#5E5340','#4A4258','#526070','#63534A'];return c[Math.floor(r*c.length)]}

/* ---------- Desenho ---------- */
function drawScene(ctx,sc,W,H,opts){
  const cam=sc.cam, cfg=sc.cfg, hy=cfg.hy*H;
  const noite = opts.noite;

  // Céu / fundo
  const g=ctx.createLinearGradient(0,0,0,hy*1.5);
  g.addColorStop(0,(CENAS[cam.cena]||CENAS.rua).ceu); g.addColorStop(1,cfg.amb);
  ctx.fillStyle=g; ctx.fillRect(0,0,W,hy+1);
  // Chão
  const g2=ctx.createLinearGradient(0,hy,0,H);
  g2.addColorStop(0,cfg.amb); g2.addColorStop(1,(CENAS[cam.cena]||CENAS.rua).chao);
  ctx.fillStyle=g2; ctx.fillRect(0,hy,W,H-hy);

  // Skyline / paredes
  ctx.save();
  if(cam.cena==='corredor'){
    ctx.fillStyle='rgba(255,255,255,.035)';
    ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(W*.3,hy*.55);ctx.lineTo(W*.3,H);ctx.lineTo(0,H);ctx.fill();
    ctx.beginPath();ctx.moveTo(W,0);ctx.lineTo(W*.7,hy*.55);ctx.lineTo(W*.7,H);ctx.lineTo(W,H);ctx.fill();
    ctx.strokeStyle='rgba(255,255,255,.08)';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(W*.3,hy*.55);ctx.moveTo(W,0);ctx.lineTo(W*.7,hy*.55);
    ctx.moveTo(0,H);ctx.lineTo(W*.3,hy*.55);ctx.moveTo(W,H);ctx.lineTo(W*.7,hy*.55);ctx.stroke();
  }else{
    const rr=mulberry(hash32(cam.codigo+'sky'));
    ctx.fillStyle='rgba(255,255,255,.045)';
    let x=0;
    while(x<W){const w=W*(.05+rr()*.09),h=hy*(.25+rr()*.7);ctx.fillRect(x,hy-h,w-1,h);
      if(noite){ctx.fillStyle='rgba(240,200,110,.30)';
        for(let wy=hy-h+3;wy<hy-3;wy+=5)for(let wx=x+2;wx<x+w-4;wx+=5)if(rr()>.62)ctx.fillRect(wx,wy,1.6,2.2);
        ctx.fillStyle='rgba(255,255,255,.045)';}
      x+=w;}
  }
  ctx.restore();

  // Pista / marcações
  if(cfg.lanes>0){
    ctx.strokeStyle='rgba(220,235,245,.18)';
    for(let l=1;l<cfg.lanes;l++){
      const u=-1+(2*l/cfg.lanes);
      ctx.setLineDash([6,10]);ctx.lineWidth=1.2;
      ctx.beginPath();
      for(let d=.16;d<=1;d+=.04){const p=sc.proj(u,d,W,H);d===.16?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y)}
      ctx.stroke();ctx.setLineDash([]);
    }
    ctx.strokeStyle='rgba(220,235,245,.26)';ctx.lineWidth=1.6;
    [-1,1].forEach(u=>{ctx.beginPath();
      for(let d=.16;d<=1;d+=.04){const p=sc.proj(u,d,W,H);d===.16?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y)}
      ctx.stroke()});
  }
  if(cam.cena==='estac'){
    ctx.strokeStyle='rgba(220,235,245,.2)';ctx.lineWidth=1.2;
    for(let i=0;i<=10;i++){const u=-1+i*.2;
      const a=sc.proj(u,.3,W,H),b=sc.proj(u*1.25,.62,W,H);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}
  }

  // Props
  sc.props.forEach(pr=>{
    const p=sc.proj(pr.u,pr.d,W,H);
    if(pr.k==='arv'){
      const h=H*.24*p.sc*(pr.s||1);
      ctx.fillStyle='rgba(28,44,36,.9)';ctx.fillRect(p.x-h*.05,p.y-h*.4,h*.1,h*.4);
      ctx.beginPath();ctx.arc(p.x,p.y-h*.55,h*.3,0,6.3);ctx.fillStyle='rgba(30,52,40,.92)';ctx.fill();
    }else if(pr.k==='poste'){
      const h=H*.30*p.sc;
      ctx.fillStyle='rgba(34,46,58,.85)';ctx.fillRect(p.x-1,p.y-h,2.2,h);
    }else if(pr.k==='lum'){
      const h=H*.42*p.sc;
      ctx.fillStyle='rgba(40,52,64,.9)';ctx.fillRect(p.x-1.2,p.y-h,2.4,h);
      ctx.beginPath();ctx.arc(p.x,p.y-h,3.2*p.sc+1.5,0,6.3);
      ctx.fillStyle='rgba(248,214,140,.5)';ctx.fill();
      const gl=ctx.createRadialGradient(p.x,p.y-h,0,p.x,p.y-h,h*.55);
      gl.addColorStop(0,'rgba(248,214,140,.16)');gl.addColorStop(1,'rgba(248,214,140,0)');
      ctx.fillStyle=gl;ctx.beginPath();ctx.arc(p.x,p.y-h,h*.55,0,6.3);ctx.fill();
    }else if(pr.k==='parked'){
      drawCar(ctx,p.x,p.y,H*.088*p.sc,pr.c,0);
    }else if(pr.k==='col'){
      const h=H*.5*p.sc;
      ctx.fillStyle='rgba(40,50,62,.8)';ctx.fillRect(p.x-2.5*p.sc-1,p.y-h,5*p.sc+2,h);
    }else if(pr.k==='porta'){
      const h=H*.34*p.sc,w=H*.12*p.sc;
      ctx.fillStyle='rgba(255,255,255,.05)';ctx.fillRect(p.x-w/2,p.y-h,w,h);
      ctx.strokeStyle='rgba(255,255,255,.1)';ctx.lineWidth=1;ctx.strokeRect(p.x-w/2,p.y-h,w,h);
    }else if(pr.k==='guarita'){
      const h=H*.3*p.sc,w=H*.26*p.sc;
      ctx.fillStyle='rgba(30,40,52,.95)';ctx.fillRect(p.x-w/2,p.y-h,w,h);
      ctx.fillStyle='rgba(120,190,230,.16)';ctx.fillRect(p.x-w/2+3,p.y-h+4,w-6,h*.42);
    }else if(pr.k==='cancela'){
      const h=H*.12*p.sc;
      ctx.fillStyle='rgba(50,60,72,.9)';ctx.fillRect(p.x-2,p.y-h,4,h);
      ctx.save();ctx.translate(p.x,p.y-h);
      ctx.fillStyle='#C8433F';ctx.fillRect(0,-2,H*.3*p.sc,4);
      ctx.fillStyle='#E8E8E8';for(let i=0;i<4;i++)ctx.fillRect(i*H*.075*p.sc+H*.035*p.sc,-2,H*.035*p.sc,4);
      ctx.restore();
    }
  });

  // Cerca do perímetro
  if(cam.cena==='perimetro'){
    ctx.strokeStyle='rgba(120,145,165,.28)';ctx.lineWidth=1;
    const a=sc.proj(-1.2,.3,W,H),b=sc.proj(1.2,.3,W,H);
    for(let k=0;k<5;k++){ctx.beginPath();ctx.moveTo(a.x,a.y-k*7);ctx.lineTo(b.x,b.y-k*7);ctx.stroke()}
  }

  // Agentes (ordenados por profundidade)
  const sorted=[...sc.agents].sort((x,y)=>x.d-y.d);
  sorted.forEach(a=>{
    const p=sc.proj(a.u,a.d,W,H);
    if(a.t==='pessoa'){
      drawPerson(ctx,p.x,p.y,H*.19*p.sc,a,sc.t);
    }else if(a.t==='motocicleta'){
      drawMoto(ctx,p.x,p.y,H*.062*p.sc,a.cor,a.vu>0?1:-1);
    }else{
      drawCar(ctx,p.x,p.y,H*.085*p.sc,a.cor,a.vu>0?1:-1,noite);
    }
    a._p=p;
  });

  return sorted;
}

function drawPerson(ctx,x,y,h,a,t){
  const w=h*.30, sw=Math.sin(t*(a.corre?9:4.5)+a.fase)*(a.parado?0:1);
  ctx.save();
  ctx.fillStyle='rgba(0,0,0,.3)';
  ctx.beginPath();ctx.ellipse(x,y,w*.75,h*.05,0,0,6.3);ctx.fill();
  // pernas
  ctx.strokeStyle=a.cor;ctx.lineWidth=Math.max(1.2,w*.26);ctx.lineCap='round';
  ctx.beginPath();ctx.moveTo(x,y-h*.42);ctx.lineTo(x+sw*w*.55,y);ctx.stroke();
  ctx.beginPath();ctx.moveTo(x,y-h*.42);ctx.lineTo(x-sw*w*.55,y);ctx.stroke();
  // tronco
  ctx.fillStyle=a.cor;
  ctx.beginPath();
  ctx.moveTo(x-w*.5,y-h*.42);ctx.lineTo(x-w*.42,y-h*.76);
  ctx.lineTo(x+w*.42,y-h*.76);ctx.lineTo(x+w*.5,y-h*.42);ctx.closePath();ctx.fill();
  if(a.mochila){ctx.fillStyle='rgba(200,110,70,.75)';ctx.fillRect(x-w*.62,y-h*.72,w*.22,h*.26)}
  // braços
  ctx.strokeStyle=a.cor;ctx.lineWidth=Math.max(1,w*.2);
  ctx.beginPath();ctx.moveTo(x-w*.45,y-h*.72);ctx.lineTo(x-w*.52-sw*w*.3,y-h*.42);ctx.stroke();
  ctx.beginPath();ctx.moveTo(x+w*.45,y-h*.72);ctx.lineTo(x+w*.52+sw*w*.3,y-h*.42);ctx.stroke();
  // cabeça
  ctx.fillStyle='#8A7565';
  ctx.beginPath();ctx.arc(x,y-h*.86,h*.115,0,6.3);ctx.fill();
  ctx.restore();
}
function drawCar(ctx,x,y,h,cor,dir,noite){
  const w=h*2.15;
  ctx.save();
  ctx.fillStyle='rgba(0,0,0,.34)';
  ctx.beginPath();ctx.ellipse(x,y,w*.5,h*.14,0,0,6.3);ctx.fill();
  ctx.fillStyle=cor;
  rr(ctx,x-w/2,y-h*.92,w,h*.62,h*.16);ctx.fill();
  rr(ctx,x-w*.30,y-h*1.30,w*.62,h*.48,h*.14);ctx.fill();
  ctx.fillStyle='rgba(150,195,225,.28)';
  rr(ctx,x-w*.26,y-h*1.24,w*.54,h*.34,h*.08);ctx.fill();
  ctx.fillStyle='#14181D';
  ctx.beginPath();ctx.arc(x-w*.29,y-h*.13,h*.19,0,6.3);ctx.fill();
  ctx.beginPath();ctx.arc(x+w*.29,y-h*.13,h*.19,0,6.3);ctx.fill();
  if(dir!==0){
    const fx=x+dir*w*.48;
    ctx.fillStyle=dir>0?'rgba(255,238,190,.85)':'rgba(255,90,80,.8)';
    ctx.fillRect(fx-2,y-h*.72,4,h*.16);
    if(noite&&dir>0){
      const gl=ctx.createRadialGradient(fx,y-h*.6,0,fx,y-h*.6,w*.9);
      gl.addColorStop(0,'rgba(255,238,190,.16)');gl.addColorStop(1,'rgba(255,238,190,0)');
      ctx.fillStyle=gl;ctx.beginPath();ctx.arc(fx,y-h*.6,w*.9,0,6.3);ctx.fill();
    }
  }
  ctx.restore();
}
function drawMoto(ctx,x,y,h,cor,dir){
  ctx.save();
  ctx.fillStyle='rgba(0,0,0,.3)';ctx.beginPath();ctx.ellipse(x,y,h*.9,h*.13,0,0,6.3);ctx.fill();
  ctx.strokeStyle='#14181D';ctx.lineWidth=Math.max(1.2,h*.13);
  ctx.beginPath();ctx.arc(x-h*.55,y-h*.28,h*.28,0,6.3);ctx.stroke();
  ctx.beginPath();ctx.arc(x+h*.55,y-h*.28,h*.28,0,6.3);ctx.stroke();
  ctx.strokeStyle=cor;ctx.lineWidth=Math.max(1.4,h*.2);
  ctx.beginPath();ctx.moveTo(x-h*.5,y-h*.4);ctx.lineTo(x+h*.15,y-h*.62);ctx.lineTo(x+h*.55,y-h*.4);ctx.stroke();
  ctx.fillStyle='#39445A';
  ctx.beginPath();ctx.moveTo(x-h*.18,y-h*.6);ctx.lineTo(x-h*.05,y-h*1.35);ctx.lineTo(x+h*.2,y-h*1.35);ctx.lineTo(x+h*.25,y-h*.6);ctx.closePath();ctx.fill();
  ctx.fillStyle='#C9D4DE';ctx.beginPath();ctx.arc(x+h*.06,y-h*1.5,h*.2,0,6.3);ctx.fill();
  ctx.restore();
}
function rr(ctx,x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath()}

/* ---------- Overlays ---------- */
function drawZonas(ctx,sc,W,H){
  (sc.cam.zonas||[]).forEach(z=>{
    const pts=z.pontos||[];
    if(pts.length<2)return;
    ctx.save();
    ctx.strokeStyle=z.cor;ctx.lineWidth=1.3;ctx.setLineDash(z.tipo==='LINHA'?[]:[5,4]);
    ctx.beginPath();
    pts.forEach((p,i)=>{const x=p[0]*W,y=p[1]*H;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});
    if(z.tipo==='POLIGONO'){ctx.closePath();ctx.fillStyle=z.cor+'14';ctx.fill()}
    ctx.stroke();ctx.setLineDash([]);
    ctx.restore();
  });
}
function drawBoxes(ctx,sc,agents,W,H,scale){
  const cam=sc.cam;
  agents.forEach(a=>{
    if(!a.track||!a._p)return;
    const p=a._p;
    let hh,ww,cor,rot;
    if(a.t==='pessoa'){hh=H*.19*p.sc;ww=hh*.46;cor='#35A7DE';rot='pessoa'}
    else if(a.t==='motocicleta'){hh=H*.115*p.sc;ww=hh*2.0;cor='#F0A02A';rot='motocicleta'}
    else{hh=H*.15*p.sc;ww=hh*1.55;cor='#2FB67C';rot='veiculo'}
    if(hh<7)return;
    const x=p.x-ww/2,y=p.y-hh;
    ctx.save();
    ctx.strokeStyle=cor;ctx.lineWidth=1.2;ctx.globalAlpha=.9;
    const c=Math.min(ww,hh)*.26;
    ctx.beginPath();
    ctx.moveTo(x,y+c);ctx.lineTo(x,y);ctx.lineTo(x+c,y);
    ctx.moveTo(x+ww-c,y);ctx.lineTo(x+ww,y);ctx.lineTo(x+ww,y+c);
    ctx.moveTo(x+ww,y+hh-c);ctx.lineTo(x+ww,y+hh);ctx.lineTo(x+ww-c,y+hh);
    ctx.moveTo(x+c,y+hh);ctx.lineTo(x,y+hh);ctx.lineTo(x,y+hh-c);
    ctx.stroke();
    ctx.globalAlpha=.10;ctx.fillStyle=cor;ctx.fillRect(x,y,ww,hh);ctx.globalAlpha=1;
    if(scale>=.55&&hh>26){
      const conf=(78+(a.id*13)%21);
      const txt=`${rot} ${conf}%`;
      ctx.font='600 8px "IBM Plex Mono",monospace';
      const tw=ctx.measureText(txt).width+7;
      ctx.fillStyle=cor;ctx.fillRect(x,y-10,tw,10);
      ctx.fillStyle='#04141E';ctx.fillText(txt,x+3.5,y-2.6);
      if(a.t==='pessoa'&&cam.analitico_facial&&a.id%3===0&&hh>40){
        ctx.strokeStyle='#F0A02A';ctx.lineWidth=1;
        const fw=ww*.42,fh=hh*.17,fx=p.x-fw/2,fy=y+hh*.02;
        ctx.strokeRect(fx,fy,fw,fh);
      }
    }
    ctx.restore();
  });
}
function drawFx(ctx,sc,W,H,noise){
  // vinheta
  const v=ctx.createRadialGradient(W/2,H/2,Math.min(W,H)*.32,W/2,H/2,Math.max(W,H)*.75);
  v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.5)');
  ctx.fillStyle=v;ctx.fillRect(0,0,W,H);
  // ruído
  if(noise){
    ctx.globalAlpha=.045;
    for(let i=0;i<W*H/260;i++){
      ctx.fillStyle=sc.rng()>.5?'#fff':'#000';
      ctx.fillRect(Math.random()*W|0,Math.random()*H|0,1,1);
    }
    ctx.globalAlpha=1;
  }
  // scanlines
  ctx.globalAlpha=.05;ctx.fillStyle='#000';
  for(let y=0;y<H;y+=3)ctx.fillRect(0,y,W,1);
  ctx.globalAlpha=1;
  // glitch
  if(sc.glitch>0){
    const gy=(sc.rng()*H)|0, gh=4+sc.rng()*14;
    try{const im=ctx.getImageData(0,gy,W,gh);ctx.putImageData(im,(sc.rng()*18-9)|0,gy)}catch(e){}
    ctx.fillStyle='rgba(53,167,222,.06)';ctx.fillRect(0,gy,W,gh);
  }
}

/* Caixas vindas do worker pelo WebSocket — cor por classe.
   Diferente de `drawBoxes`, que desenha os agentes inventados pela cena
   sintética, estas representam o que o modelo realmente detectou. */
function pintarCaixasReais(ctx,lista,W,H){
  if(!lista.length)return;
  const s=W/640;
  ctx.save();
  ctx.lineWidth=Math.max(1.4,2*s);
  ctx.font=`600 ${Math.max(8,9.5*s)}px "IBM Plex Sans",system-ui,sans-serif`;
  ctx.textBaseline='middle';
  lista.forEach(c=>{
    const b=c.bbox;
    if(!b||b.length<4)return;
    // bbox normalizado é [x, y, largura, altura] — não [x1, y1, x2, y2].
    const x=b[0]*W,y=b[1]*H,w=b[2]*W,h=b[3]*H;
    const cor=COR_CLASSE[c.classe]||COR_CLASSE.FACIAL;
    ctx.strokeStyle=cor;
    ctx.strokeRect(x,y,w,h);

    const rot=(c.classe?(ROTULO_OBJ[c.classe]||c.classe):'Rosto').toUpperCase();
    const pad=3*s, alt=13*s, larg=ctx.measureText(rot).width+pad*2;
    ctx.fillStyle=cor;
    ctx.fillRect(x,Math.max(0,y-alt),larg,alt);
    ctx.fillStyle='#08121A';
    ctx.fillText(rot,x+pad,Math.max(0,y-alt)+alt/2);
  });
  ctx.restore();
}

/* Renderiza nomes de pessoas identificadas sobre a câmera */
function pintarIdentificacoes(ctx,lista,W,H){
  if(!lista.length)return;
  const s=W/640;
  ctx.save();
  ctx.font=`700 ${Math.max(11,12*s)}px "IBM Plex Sans",system-ui,sans-serif`;
  ctx.textBaseline='middle';
  lista.forEach(id=>{
    const b=id.bbox;
    if(!b||b.length<4)return;
    const x=b[0]*W,y=b[1]*H,w=b[2]*W,h=b[3]*H;
    const nome=id.pessoa_nome.toUpperCase();
    const pad=4*s, alt=16*s, larg=ctx.measureText(nome).width+pad*2;
    // Renderiza acima da caixa com cor destaque
    ctx.fillStyle='rgba(240,160,42,0.9)';  // Laranja SEGURANÇA
    ctx.fillRect(x,Math.max(0,y-alt-2),larg,alt);
    ctx.fillStyle='#08121A';
    ctx.fillText(nome,x+pad,Math.max(0,y-alt/2-1));
  });
  ctx.restore();
}

/* Recorte guardado da captura: cabe inteiro e não é ampliado além do que a
   resolução aguenta. Esticar um recorte de 49x85 até 500px não cria detalhe
   nenhum — só vira um borrão que parece defeito do sistema. */
function pintarRecorte(ctx,img,W,H){
  const MAX_AMPLIACAO=3;
  const esc=Math.min(W/img.naturalWidth,H/img.naturalHeight,MAX_AMPLIACAO);
  const w=img.naturalWidth*esc,h=img.naturalHeight*esc;
  ctx.fillStyle='#0A1218';
  ctx.fillRect(0,0,W,H);
  ctx.drawImage(img,(W-w)/2,(H-h)/2,w,h);
}

/* Quadro ao vivo cobrindo o tile, sem distorcer: recorta o excedente.

   Com `conter`, nada é recortado — o quadro inteiro cabe na área. É o que a
   ficha da câmera precisa: quem vai ajustar enquadramento tem de ver onde o
   campo de visão termina, e o corte do mosaico escondia justamente as bordas. */
function pintarAoVivo(ctx,img,W,H,conter){
  const esc=conter?Math.min(W/img.naturalWidth,H/img.naturalHeight)
                  :Math.max(W/img.naturalWidth,H/img.naturalHeight);
  const w=img.naturalWidth*esc,h=img.naturalHeight*esc;
  if(conter){ctx.fillStyle='#000';ctx.fillRect(0,0,W,H)}
  ctx.drawImage(img,(W-w)/2,(H-h)/2,w,h);
}

/* ---------- Motor ---------- */
const FEED={
  scenes:new Map(),
  tiles:[],
  running:false,
  last:0,
  frames:0,
  fpsT:0,
  noite:(()=>{const h=new Date().getHours();return h>=18||h<6})(),
  fps:0,
  aoMedirFps:null,
  watermark:true,
  mostrarZonas:true,
  mostrarCaixas:true,
  fonteAoVivo:null,    // (cam) => URL do MJPEG, ou null para a cena sintética
  fonteSnapshot:null,  // (cam) => URL de um quadro único, para pré-visualização
  caixas:new Map(),    // código da câmera => caixas reais ainda vivas
  identificacoes:new Map(),  // código da câmera => pessoas identificadas

  definirCaixas(codigo,lista,validadeS){
    // Substitui o conjunto inteiro: é a cena atual, não um acúmulo.
    // A validade vem do worker, que é quem sabe quando volta a esta câmera.
    const vida=validadeS?validadeS*1000:CAIXA_VIDA_MS;
    this.caixas.set(codigo,{ate:Date.now()+vida,lista:lista||[]});
  },
  caixasDe(codigo){
    const reg=this.caixas.get(codigo);
    // Se o worker parar de publicar, as caixas somem. Melhor nenhuma caixa
    // que uma caixa parada sobre um objeto que já saiu de cena.
    return (reg&&reg.ate>Date.now())?reg.lista:[];
  },
  adicionarIdentificacao(codigo,dados){
    // Armazena identificação por 3 segundos
    this.identificacoes.set(codigo,{ate:Date.now()+3000,dados:dados});
  },
  identificacoesDe(codigo){
    const reg=this.identificacoes.get(codigo);
    return (reg&&reg.ate>Date.now())?[reg.dados]:[];
  },

  scene(cam){
    if(!this.scenes.has(cam.codigo))this.scenes.set(cam.codigo,new Scene(cam));
    return this.scenes.get(cam.codigo);
  },
  register(canvas,cam,opts){
    const t={canvas,ctx:canvas.getContext('2d',{alpha:false}),cam,opts:opts||{},
             img:null,modo:null,poll:null};
    this.tiles.push(t);
    this.ligarFonte(t);
  },
  /* O MJPEG segura a conexão HTTP aberta enquanto o vídeo durar, e o
     navegador só permite seis por origem. Com o parque maior que isso os
     tiles excedentes ficavam pretos para sempre — a requisição nunca saía da
     fila, e nada no servidor acusava erro porque ela nunca chegou lá.
     Os primeiros ficam no fluxo contínuo; os demais recarregam um quadro por
     vez, em conexões que fecham e devolvem a vaga. */
  ligarFonte(t){
    const emFluxo=this.tiles.filter(x=>x.modo==='mjpeg').length;
    // A ficha e a tela cheia são um tile só, e é onde a fluidez importa.
    if((t.opts&&t.opts.hd)||emFluxo<MAX_MJPEG){
      const url=this.fonteAoVivo?this.fonteAoVivo(t.cam,t.opts):null;
      if(!url)return;
      t.modo='mjpeg';
      const img=new Image();
      img.onerror=()=>{t.img=null};   // câmera muda: volta para a cena sintética
      img.src=url;
      t.img=img;
      return;
    }
    const url=this.fonteSnapshot?this.fonteSnapshot(t.cam):null;
    if(!url)return;
    t.modo='poll';
    const puxar=()=>{
      if(!t.canvas||!t.canvas.isConnected){clearInterval(t.poll);t.poll=null;return}
      const img=new Image();
      // Só troca o quadro exibido quando o novo chegou inteiro: atribuir
      // antes deixaria o tile piscar em branco a cada recarga.
      img.onload=()=>{t.img=img};
      img.onerror=()=>{};
      img.src=url+'&t='+Date.now();
    };
    puxar();
    t.poll=setInterval(puxar,POLL_MS);
  },
  soltar(t){
    if(t.poll){clearInterval(t.poll);t.poll=null}
    if(t.img){t.img.onerror=null;t.img.onload=null;t.img.src='';t.img=null}
  },
  /* Tile cujo canvas saiu do documento — modal fechado, lista redesenhada.
     Sem soltar o `src`, a conexão MJPEG continua aberta e o leitor RTSP do
     servidor segue vivo, alimentando um canvas que ninguém vê. Na ficha da
     câmera isso custava caro: o fluxo em resolução nativa não terminava. */
  podar(){
    const vivos=[];
    this.tiles.forEach(t=>{
      if(t.canvas&&t.canvas.isConnected){vivos.push(t);return}
      this.soltar(t);
    });
    this.tiles=vivos;
  },
  clear(){
    // Soltar o src encerra a conexão MJPEG. Sem isto cada troca de tela
    // deixaria um leitor RTSP pendurado no servidor.
    this.tiles.forEach(t=>this.soltar(t));
    this.tiles=[];
  },
  resize(t){
    const r=t.canvas.getBoundingClientRect();
    if(!r.width||!r.height)return false;
    const dpr=Math.min(window.devicePixelRatio||1,2);
    // 900px é o teto do mosaico, onde cada tile é pequeno e são muitos. Na
    // ficha da câmera esse mesmo teto era o gargalo final: o quadro nativo
    // chegava inteiro e era reamostrado para 900 antes de aparecer.
    const teto=t.opts.hd?2560:900;
    const w=Math.min(Math.round(r.width*dpr),teto), h=Math.round(w*r.height/r.width);
    if(t.canvas.width!==w||t.canvas.height!==h){t.canvas.width=w;t.canvas.height=h}
    return true;
  },
  loop(ts){
    if(!FEED.running)return;
    requestAnimationFrame(FEED.loop);
    const dt=Math.min((ts-FEED.last)/1000,.12);
    if(dt<1/22)return;
    FEED.last=ts;
    FEED.frames++;
    if(ts-FEED.fpsT>1000){FEED.fps=FEED.frames;FEED.frames=0;FEED.fpsT=ts;if(FEED.aoMedirFps)FEED.aoMedirFps(FEED.fps)}
    if(document.hidden)return;
    FEED.podar();

    const seen=new Set();
    FEED.tiles.forEach(t=>{
      if(!FEED.resize(t))return;
      const sc=FEED.scene(t.cam);
      if(!seen.has(t.cam.codigo)){sc.step(dt);seen.add(t.cam.codigo)}
      const ctx=t.ctx,W=t.canvas.width,H=t.canvas.height;
      const vivo=t.img&&t.img.naturalWidth>0;
      // Mostra vídeo real quando carregado; enquanto carrega, canvas fica escuro
      if(vivo){
        pintarAoVivo(ctx,t.img,W,H,t.opts.conter);
        if(t.opts.zonas!==false&&FEED.mostrarZonas)drawZonas(ctx,sc,W,H);
      }else{
        ctx.fillStyle='#0A0E15';
        ctx.fillRect(0,0,W,H);
      }
      // Detecções (caixas e nomes) são sempre mostradas, independente do vídeo
      if(t.opts.boxes!==false&&FEED.mostrarCaixas)pintarCaixasReais(ctx,FEED.caixasDe(t.cam.codigo),W,H);
      pintarIdentificacoes(ctx,FEED.identificacoesDe(t.cam.codigo),W,H);
      if(t.opts.osd!==false)drawOSD(ctx,t.cam,W,H);
    });
  },
  start(){if(this.running)return;this.running=true;this.last=performance.now();requestAnimationFrame(this.loop)},
  stop(){this.running=false}
};

function drawOSD(ctx,cam,W,H){
  const s=W/640;
  ctx.save();
  ctx.font=`500 ${Math.max(8,9*s)}px "IBM Plex Mono",monospace`;
  ctx.fillStyle='rgba(255,255,255,.85)';

  // Nome e código da câmera, canto inferior esquerdo
  ctx.textAlign='left';
  const label = `${cam.nome.toUpperCase()} - ${cam.codigo}`;
  ctx.fillText(label, 8*s, H - 10*s);

  // Data e hora, canto superior direito
  ctx.textAlign='right';
  const agora = new Date();
  const data = agora.toLocaleDateString('pt-BR');
  const hora = agora.toLocaleTimeString('pt-BR');
  ctx.fillText(`${data} ${hora}`, W - 8*s, 16*s);

  ctx.restore();
}

/* ---------- Retrato biométrico (template, não fotografia) ---------- */
function drawBioFace(canvas,seed,sexo,size){
  const dpr=Math.min(window.devicePixelRatio||1,2);
  const S=size||44;
  canvas.width=S*dpr;canvas.height=S*dpr;
  canvas.style.width=S+'px';canvas.style.height=S+'px';
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const r=mulberry(seed>>>0);
  const cor=SEXOS[sexo]?SEXOS[sexo].cor:'#5F7589';
  const g=ctx.createLinearGradient(0,0,S,S);
  g.addColorStop(0,'#0E1821');g.addColorStop(1,'#16232E');
  ctx.fillStyle=g;ctx.fillRect(0,0,S,S);
  const cx=S/2, cy=S*.54, hw=S*.24+r()*S*.03, hh=S*.31+r()*S*.03;
  ctx.save();
  ctx.beginPath();ctx.ellipse(cx,cy,hw,hh,0,0,6.3);
  ctx.fillStyle='rgba(255,255,255,.05)';ctx.fill();
  ctx.strokeStyle=cor;ctx.globalAlpha=.55;ctx.lineWidth=.9;ctx.stroke();ctx.globalAlpha=1;
  // malha de landmarks
  const pts=[];
  for(let i=0;i<7;i++){const a=Math.PI*(.15+i*.115);pts.push([cx-Math.cos(a)*hw*.93,cy+Math.sin(a)*hh*.9-hh*.1])}
  for(let i=0;i<7;i++){const a=Math.PI*(.15+i*.115);pts.push([cx+Math.cos(a)*hw*.93,cy+Math.sin(a)*hh*.9-hh*.1])}
  pts.push([cx-hw*.42,cy-hh*.22],[cx+hw*.42,cy-hh*.22]);           // olhos
  pts.push([cx,cy+hh*.06],[cx-hw*.13,cy+hh*.14],[cx+hw*.13,cy+hh*.14]); // nariz
  pts.push([cx-hw*.3,cy+hh*.42],[cx,cy+hh*.47],[cx+hw*.3,cy+hh*.42]);   // boca
  pts.push([cx-hw*.55,cy-hh*.5],[cx+hw*.55,cy-hh*.5],[cx,cy-hh*.62]);   // testa
  ctx.strokeStyle=cor;ctx.globalAlpha=.30;ctx.lineWidth=.55;
  for(let i=0;i<pts.length;i++){
    for(let j=i+1;j<pts.length;j++){
      const dx=pts[i][0]-pts[j][0],dy=pts[i][1]-pts[j][1];
      if(Math.hypot(dx,dy)<S*.23){ctx.beginPath();ctx.moveTo(pts[i][0],pts[i][1]);ctx.lineTo(pts[j][0],pts[j][1]);ctx.stroke()}
    }
  }
  ctx.globalAlpha=1;ctx.fillStyle=cor;
  pts.forEach(p=>{ctx.beginPath();ctx.arc(p[0],p[1],S*.018,0,6.3);ctx.fill()});
  ctx.restore();
  // cantoneiras
  ctx.strokeStyle=cor;ctx.globalAlpha=.75;ctx.lineWidth=1;
  const m=S*.09,c=S*.14;
  ctx.beginPath();
  ctx.moveTo(m,m+c);ctx.lineTo(m,m);ctx.lineTo(m+c,m);
  ctx.moveTo(S-m-c,m);ctx.lineTo(S-m,m);ctx.lineTo(S-m,m+c);
  ctx.moveTo(S-m,S-m-c);ctx.lineTo(S-m,S-m);ctx.lineTo(S-m-c,S-m);
  ctx.moveTo(m+c,S-m);ctx.lineTo(m,S-m);ctx.lineTo(m,S-m-c);
  ctx.stroke();ctx.globalAlpha=1;
}

/* ---------- Miniatura de captura ---------- */
function drawSnapshot(canvas,det,size){
  const dpr=Math.min(window.devicePixelRatio||1,2);
  const W=size||56,H=Math.round(W*.72);
  canvas.width=W*dpr;canvas.height=H*dpr;
  canvas.style.width=W+'px';canvas.style.height=H+'px';
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  // `det` precisa de: codigo, tipo, cena, bbox e (para objetos) classe.
  const cena=det.cena||'rua';
  const r=mulberry(hash32(det.codigo||'x'));
  const cfg=SCENE_CFG[cena]||SCENE_CFG.rua, hy=cfg.hy*H;
  const cores=CENAS[cena]||CENAS.rua;
  const g=ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,cores.ceu);g.addColorStop(.45,cfg.amb);g.addColorStop(1,cores.chao);
  ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
  ctx.fillStyle='rgba(255,255,255,.05)';
  let x=0;while(x<W){const w=W*(.1+r()*.16),h=hy*(.3+r()*.7);ctx.fillRect(x,hy-h,w-.7,h);x+=w}
  const bx=det.bbox?det.bbox[0]*W:W*.35, by=det.bbox?det.bbox[1]*H:H*.35;
  const bw=Math.max(10,(det.bbox?det.bbox[2]:.2)*W), bh=Math.max(12,(det.bbox?det.bbox[3]:.3)*H);
  if(det.tipo==='FACIAL'){
    ctx.fillStyle='#8A7565';ctx.beginPath();ctx.arc(bx+bw/2,by+bh*.4,bw*.34,0,6.3);ctx.fill();
    ctx.fillStyle='#46545F';ctx.fillRect(bx+bw*.12,by+bh*.66,bw*.76,bh*.4);
  }else{
    ctx.fillStyle=COR_CLASSE[det.classe]||'#4A5568';
    rr(ctx,bx,by+bh*.3,bw,bh*.7,2);ctx.fill();
  }
  const cor=det.tipo==='FACIAL'?'#F0A02A':det.tipo==='ACAO'?'#8C6FE0'
            :(COR_CLASSE[det.classe]||'#35A7DE');
  ctx.strokeStyle=cor;ctx.lineWidth=1.1;ctx.strokeRect(bx,by,bw,bh);
  ctx.fillStyle=cor;ctx.globalAlpha=.12;ctx.fillRect(bx,by,bw,bh);ctx.globalAlpha=1;
  ctx.globalAlpha=.06;ctx.fillStyle='#000';
  for(let y=0;y<H;y+=2)ctx.fillRect(0,y,W,1);
  ctx.globalAlpha=1;
  const v=ctx.createRadialGradient(W/2,H/2,W*.2,W/2,H/2,W*.75);
  v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.45)');
  ctx.fillStyle=v;ctx.fillRect(0,0,W,H);
}
