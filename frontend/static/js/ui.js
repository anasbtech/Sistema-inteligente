/* ==========================================================
   VIGIA — Utilidades de interface
   Formatação, ícones, toasts, modais, gráficos SVG e tabelas.
   ========================================================== */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const esc = s => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const ico = (n, sz = 16) => `<svg width="${sz}" height="${sz}" aria-hidden="true"><use href="#i-${n}"/></svg>`;

/* ---------- Formatação ---------- */
const pad = (n, l = 2) => String(n).padStart(l, '0');
const dt = v => (v instanceof Date ? v : new Date(v));
const fmtTime = v => { const d = dt(v); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const fmtDate = v => { const d = dt(v); return `${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()}`; };
const fmtDT = v => `${fmtDate(v)} ${fmtTime(v)}`;
const fmtNum = n => Number(n || 0).toLocaleString('pt-BR');
const fmtPct = n => Number(n || 0).toFixed(1).replace('.', ',') + '%';
const pct100 = n => (Number(n || 0) * 100).toFixed(1).replace('.', ',') + '%';

function relTime(v){
  const s = Math.floor((Date.now() - dt(v).getTime()) / 1000);
  if(s < 10)    return 'agora';
  if(s < 60)    return `há ${s}s`;
  if(s < 3600)  return `há ${Math.floor(s/60)}min`;
  if(s < 86400) return `há ${Math.floor(s/3600)}h`;
  return `há ${Math.floor(s/86400)}d`;
}
function duracao(s){
  s = Math.max(0, Math.round(s || 0));
  if(s < 60) return `${s}s`;
  const m = Math.floor(s/60), r = s % 60;
  return r ? `${m}min ${r}s` : `${m}min`;
}

/* ---------- Rótulos do domínio ---------- */
const STATUS_PESSOA = {
  PROCURADO:    {lbl:'Procurado',      cls:'c-red',    desc:'Mandado de prisão em aberto'},
  MONITORADO:   {lbl:'Monitorado',     cls:'c-amber',  desc:'Medida cautelar / restrição judicial'},
  DESAPARECIDO: {lbl:'Desaparecido',   cls:'c-violet', desc:'Registro de desaparecimento ativo'},
  FUNCIONARIO:  {lbl:'Servidor',       cls:'c-blue',   desc:'Servidor público credenciado'},
  CIDADAO:      {lbl:'Sem restrição',  cls:'c-gray',   desc:'Nenhuma restrição registrada'}
};
const STATUS_CAMERA = {
  ONLINE:     {lbl:'On-line',     cls:'c-green'},
  OFFLINE:    {lbl:'Sem sinal',   cls:'c-red'},
  MANUTENCAO: {lbl:'Manutenção',  cls:'c-amber'}
};
const STATUS_ALERTA = {
  ABERTO:       {lbl:'Aberto',   cls:'c-red'},
  EM_ANDAMENTO: {lbl:'Em curso', cls:'c-amber'},
  RESOLVIDO:    {lbl:'Resolvido',cls:'c-green'}
};
const ROTULO_OBJ = {
  pessoa:'Pessoa', carro:'Carro', caminhao:'Caminhão', onibus:'Ônibus',
  motocicleta:'Motocicleta', bicicleta:'Bicicleta', animal:'Animal',
  mochila:'Mochila', volume:'Volume abandonado', capacete:'Capacete',
  placa:'Placa veicular', arma:'Objeto tipo arma',
  veiculo:'Veículo'   // registros anteriores à separação por tipo
};
const ROTULO_ACAO_UI = {
  corrida:'Corrida súbita', queda:'Queda de pessoa', aglomeracao:'Aglomeração',
  briga:'Agressão física', permanencia:'Permanência prolongada',
  invasao:'Invasão de perímetro', contrafluxo:'Deslocamento em contrafluxo',
  abandono:'Abandono de objeto', vandalismo:'Dano ao patrimônio'
};
const SEVERIDADE = ['', 'Informativo', 'Baixa', 'Média', 'Crítica'];
const CLS_SEV = ['', 'c-gray', 'c-blue', 'c-amber', 'c-red'];

const rotuloClasse = d => d.tipo === 'ACAO'
  ? (ROTULO_ACAO_UI[d.classe] || d.classe)
  : (ROTULO_OBJ[d.classe] || d.classe);

function tituloDeteccao(d){
  if(d.tipo === 'FACIAL') return d.pessoa ? d.pessoa.nome : 'Face não identificada';
  return rotuloClasse(d);
}

/* ---------- Toasts ---------- */
function toast(titulo, msg, tipo = 'info', ms = 4200){
  const t = document.createElement('div');
  t.className = 'toast ' + tipo;
  const icone = {ok:'i-check', err:'i-alert', warn:'i-alert', info:'i-info'}[tipo];
  const cor   = {ok:'var(--green)', err:'var(--red)', warn:'var(--amber)', info:'var(--acc)'}[tipo];
  t.innerHTML = `<svg width="16" height="16" style="color:${cor};flex:none;margin-top:1px"><use href="#${icone}"/></svg>
    <div style="min-width:0"><b>${esc(titulo)}</b>${msg ? `<p>${esc(msg)}</p>` : ''}</div>`;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 240); }, ms);
}
const erroToast = (e, contexto) =>
  toast(contexto || 'Não foi possível concluir', e && e.message ? e.message : String(e), 'err', 6000);

/* ---------- Modal ---------- */
function abrirModal(html, largo){
  const box = $('#modalBox');
  box.className = 'modal' + (largo ? ' wide' : '');
  box.innerHTML = html;
  $('#modalScrim').hidden = false;
  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  document.addEventListener('keydown', _escFecha);
  return box;
}
function fecharModal(){
  $('#modalScrim').hidden = true;
  $('#modalBox').innerHTML = '';
  document.removeEventListener('keydown', _escFecha);
}
function _escFecha(e){ if(e.key === 'Escape') fecharModal(); }

/* Confirmação em modal — `confirm()` nativo é bloqueado em alguns contextos
   e não combina com o resto da interface. */
function confirmar(titulo, mensagem, {rotulo = 'Confirmar', perigo = false, exigeTexto = null} = {}){
  return new Promise(resolve => {
    abrirModal(`
      <div class="modal-hd"><h3>${esc(titulo)}</h3><span class="sp" style="flex:1"></span>
        <button class="x" data-close>${ico('x',17)}</button></div>
      <div class="modal-bd">
        <p style="margin:0;color:var(--txt-2);font-size:13.5px;line-height:1.55">${mensagem}</p>
        ${exigeTexto ? `<div class="field" style="margin-top:14px">
          <label for="cfMotivo">${esc(exigeTexto)}</label>
          <input class="inp" id="cfMotivo" placeholder="Mínimo de 8 caracteres">
        </div>` : ''}
      </div>
      <div class="modal-ft">
        <button class="btn btn-ghost" id="cfNao">Cancelar</button>
        <button class="btn ${perigo ? 'btn-danger' : 'btn-primary'}" id="cfSim">${esc(rotulo)}</button>
      </div>`);
    const terminar = v => { fecharModal(); resolve(v); };
    $('#cfNao').onclick = () => terminar(null);
    $('#modalBox').querySelector('.x').onclick = () => terminar(null);
    $('#cfSim').onclick = () => {
      if(exigeTexto){
        const v = $('#cfMotivo').value.trim();
        if(v.length < 8){ toast('Justificativa obrigatória', 'Descreva o motivo com ao menos 8 caracteres.', 'err'); return; }
        return terminar(v);
      }
      terminar(true);
    };
  });
}

/* ---------- Estados de carregamento ---------- */
const carregando = (msg = 'Carregando…') =>
  `<div class="empty"><p class="mono" style="color:var(--txt-3)">${esc(msg)}</p></div>`;

const vazio = (icone, titulo, sub) =>
  `<div class="empty">${ico(icone, 30)}<p>${esc(titulo)}</p>${sub ? `<small>${esc(sub)}</small>` : ''}</div>`;

const erroBloco = (e, aoTentar) => `<div class="empty">${ico('alert',30)}
  <p>Não foi possível carregar.</p><small>${esc(e && e.message ? e.message : e)}</small>
  ${aoTentar ? `<div style="margin-top:12px"><button class="btn btn-ghost btn-sm" id="${aoTentar}">Tentar de novo</button></div>` : ''}</div>`;

/* ---------- Gráficos SVG ---------- */
function areaChart(series, opts){
  const o = Object.assign({w:720, h:200, pad:{t:14,r:12,b:26,l:38}, labels:[], yTicks:4}, opts || {});
  const {w, h, pad} = o, iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  const max = Math.max(1, ...series.flatMap(s => s.data));
  const n = Math.max(1, (series[0] ? series[0].data.length : 1) - 1);
  const step = iw / n;
  const X = i => pad.l + i * step, Y = v => pad.t + ih - (v / max) * ih;

  let grade = '';
  for(let i = 0; i <= o.yTicks; i++){
    const v = max * i / o.yTicks, y = Y(v);
    grade += `<line x1="${pad.l}" y1="${y.toFixed(1)}" x2="${w-pad.r}" y2="${y.toFixed(1)}" stroke="var(--line-soft)" stroke-width="1"/>`;
    grade += `<text x="${pad.l-7}" y="${(y+3.5).toFixed(1)}" text-anchor="end" font-size="9.5" font-family="IBM Plex Mono, monospace" fill="var(--txt-4)">${Math.round(v)}</text>`;
  }
  let linhas = '';
  series.forEach(s => {
    if(!s.data.length) return;
    const pts = s.data.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    linhas += `<polygon points="${pad.l},${pad.t+ih} ${pts} ${w-pad.r},${pad.t+ih}" fill="${s.cor}" opacity=".13"/>`;
    linhas += `<polyline points="${pts}" fill="none" stroke="${s.cor}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>`;
    const u = s.data.length - 1;
    linhas += `<circle cx="${X(u).toFixed(1)}" cy="${Y(s.data[u]).toFixed(1)}" r="3" fill="${s.cor}" stroke="var(--ink-850)" stroke-width="1.5"/>`;
  });
  let rotulos = '';
  o.labels.forEach((l, i) => {
    if(l) rotulos += `<text x="${X(i).toFixed(1)}" y="${h-8}" text-anchor="middle" font-size="9.5" font-family="IBM Plex Mono, monospace" fill="var(--txt-4)">${esc(l)}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block" role="img">${grade}${linhas}${rotulos}</svg>`;
}

function barsChart(items, opts){
  const o = Object.assign({w:720, h:210, pad:{t:16,r:12,b:44,l:36}}, opts || {});
  const {w, h, pad} = o, iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  if(!items.length) return `<div class="empty"><p>Sem dados no período.</p></div>`;
  const max = Math.max(1, ...items.map(i => i.v));
  const bw = iw / items.length, bar = Math.min(bw * .56, 40);
  let out = '';
  for(let i = 0; i <= 4; i++){
    const y = pad.t + ih - ih * i / 4;
    out += `<line x1="${pad.l}" y1="${y.toFixed(1)}" x2="${w-pad.r}" y2="${y.toFixed(1)}" stroke="var(--line-soft)"/>`;
    out += `<text x="${pad.l-6}" y="${(y+3.5).toFixed(1)}" text-anchor="end" font-size="9.5" font-family="IBM Plex Mono, monospace" fill="var(--txt-4)">${Math.round(max*i/4)}</text>`;
  }
  items.forEach((it, i) => {
    const bh = (it.v / max) * ih, x = pad.l + i * bw + (bw - bar) / 2, y = pad.t + ih - bh;
    out += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bar.toFixed(1)}" height="${Math.max(1,bh).toFixed(1)}" rx="2" fill="${it.cor || 'var(--acc)'}" opacity=".85"/>`;
    out += `<text x="${(x+bar/2).toFixed(1)}" y="${(y-4).toFixed(1)}" text-anchor="middle" font-size="9.5" font-weight="600" font-family="IBM Plex Mono, monospace" fill="var(--txt-2)">${it.v}</text>`;
    const palavras = String(it.k).split(' ');
    out += `<text x="${(x+bar/2).toFixed(1)}" y="${h-24}" text-anchor="middle" font-size="9" fill="var(--txt-3)">${esc(palavras[0].slice(0,13))}</text>`;
    if(palavras[1]) out += `<text x="${(x+bar/2).toFixed(1)}" y="${h-13}" text-anchor="middle" font-size="9" fill="var(--txt-4)">${esc(palavras.slice(1).join(' ').slice(0,13))}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block" role="img">${out}</svg>`;
}

function donut(parts, opts){
  const o = Object.assign({size:176, thick:24, centro:'', sub:''}, opts || {});
  const S = o.size, R = S/2 - 4, r = R - o.thick, cx = S/2, cy = S/2;
  const tot = parts.reduce((s, p) => s + p.v, 0) || 1;
  let a0 = -Math.PI/2, out = '';
  parts.forEach(p => {
    if(p.v <= 0) return;
    const a1 = a0 + (p.v/tot) * Math.PI * 2;
    const grande = (a1 - a0) > Math.PI ? 1 : 0;
    const x0 = cx + R*Math.cos(a0), y0 = cy + R*Math.sin(a0);
    const x1 = cx + R*Math.cos(a1), y1 = cy + R*Math.sin(a1);
    const x2 = cx + r*Math.cos(a1), y2 = cy + r*Math.sin(a1);
    const x3 = cx + r*Math.cos(a0), y3 = cy + r*Math.sin(a0);
    out += `<path d="M${x0.toFixed(2)},${y0.toFixed(2)} A${R},${R} 0 ${grande} 1 ${x1.toFixed(2)},${y1.toFixed(2)} L${x2.toFixed(2)},${y2.toFixed(2)} A${r},${r} 0 ${grande} 0 ${x3.toFixed(2)},${y3.toFixed(2)} Z" fill="${p.cor}"/>`;
    a0 = a1;
  });
  out += `<text x="${cx}" y="${cy-1}" text-anchor="middle" font-size="26" font-weight="600" font-family="IBM Plex Mono, monospace" fill="var(--txt-1)">${esc(o.centro)}</text>`;
  out += `<text x="${cx}" y="${cy+16}" text-anchor="middle" font-size="9.5" letter-spacing="1.2" fill="var(--txt-3)">${esc(o.sub)}</text>`;
  return `<svg viewBox="0 0 ${S} ${S}" style="width:${S}px;max-width:100%;height:auto;display:block" role="img">${out}</svg>`;
}

/* ---------- Paginação ---------- */
function pagerHTML(total, pagina, porPagina, idPrefixo){
  const paginas = Math.max(1, Math.ceil(total / porPagina));
  const ini = total ? (pagina - 1) * porPagina + 1 : 0;
  const fim = Math.min(total, pagina * porPagina);
  const janela = [];
  for(let i = 1; i <= paginas; i++){
    if(i <= 2 || i > paginas - 2 || Math.abs(i - pagina) <= 1) janela.push(i);
  }
  let btns = '', ultimo = 0;
  janela.forEach(i => {
    if(i - ultimo > 1) btns += '<span style="color:var(--txt-4);padding:0 2px">…</span>';
    btns += `<button class="pg ${i === pagina ? 'on' : ''}" data-pg="${idPrefixo}:${i}">${i}</button>`;
    ultimo = i;
  });
  return `<div class="pager">
    <span class="info">${fmtNum(ini)}–${fmtNum(fim)} de ${fmtNum(total)}</span>
    <span class="sp"></span>
    <button class="pg" data-pg="${idPrefixo}:${Math.max(1, pagina-1)}" ${pagina <= 1 ? 'disabled' : ''}>${ico('lt',13)}</button>
    ${btns}
    <button class="pg" data-pg="${idPrefixo}:${Math.min(paginas, pagina+1)}" ${pagina >= paginas ? 'disabled' : ''}>${ico('chev',13)}</button>
  </div>`;
}

/* ---------- Debounce para campos de busca ---------- */
function debounce(fn, ms = 320){
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* ---------- Desenho diferido dos canvas ---------- */
function pintarFaces(raiz){
  $$('[data-face]', raiz || document).forEach(c => {
    if(c.dataset.pintado) return;
    drawBioFace(c, hash32(c.dataset.face), c.dataset.sexo, +(c.getAttribute('width') || 38));
    c.dataset.pintado = '1';
  });
}
/* Busca o recorte guardado da captura e o desenha sobre o canvas.

   O desenho sintético continua por baixo: quando não há imagem — detecção
   antiga, ou vinda do simulador — fica o esquema, que não finge ser uma
   fotografia de alguém. */
function carregarRecorte(canvas, codigo){
  if(!codigo || !API.access) return;
  const img = new Image();
  img.onload  = () => pintarRecorte(canvas.getContext('2d', {alpha:false}), img, canvas.width, canvas.height);
  img.onerror = () => {};
  img.src = `${API.base}/deteccoes/${encodeURIComponent(codigo)}/snapshot`
          + `?token=${encodeURIComponent(API.access)}`;
}

function pintarSnaps(raiz){
  $$('[data-snap]', raiz || document).forEach(c => {
    if(c.dataset.pintado) return;
    let d;
    try{ d = JSON.parse(c.dataset.snap); }catch(e){ return; }
    drawSnapshot(c, d, +(c.getAttribute('width') || 56));
    c.dataset.pintado = '1';
    carregarRecorte(c, d.codigo);
  });
}
/* Serializa só o que a miniatura precisa — evita jogar o objeto inteiro no DOM */
const snapAttr = d => esc(JSON.stringify({
  codigo: d.codigo, tipo: d.tipo, classe: d.classe, bbox: d.bbox,
  cena: (d.camera && d.camera.cena) || d.cena || 'rua'
}));
