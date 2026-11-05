/* ==========================================================
   SEGURANÇA — Utilidades de interface
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
/* O que <input type="date"> lê e devolve. Pelos campos locais, e não por
   toISOString(): perto da meia-noite o UTC já virou o dia seguinte. */
const diaISO = v => { const d = dt(v); return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`; };
const fmtDT = v => `${fmtDate(v)} ${fmtTime(v)}`;
const fmtNum = n => Number(n || 0).toLocaleString('pt-BR');
const fmtPct = n => Number(n || 0).toFixed(1).replace('.', ',') + '%';
const pct100 = n => (Number(n || 0) * 100).toFixed(1).replace('.', ',') + '%';
function pctCurto(v, total){
  if(!total || v <= 0) return '0%';
  const p = v / total * 100;
  if(p < 0.1) return '<0,1%';
  if(p < 10) return p.toFixed(1).replace('.', ',') + '%';
  return Math.round(p) + '%';
}


function relTime(v){
  const s = Math.floor((Date.now() - dt(v).getTime()) / 1000);
  if(s < 10)    return 'agora';
  if(s < 60)    return `há ${s}s`;
  if(s < 3600)  return `há ${Math.floor(s/60)}min`;
  if(s < 86400) return `há ${Math.floor(s/3600)}h`;
  return `há ${Math.floor(s/86400)}d`;
}
/* Duração legível em qualquer escala. Só em minutos, o tempo médio de
   resposta da Central de alertas saía "11713min 2s" — oito dias que ninguém
   lê como oito dias. As duas maiores unidades bastam. */
function duracao(s){
  s = Math.max(0, Math.round(s || 0));
  if(s < 60) return `${s}s`;
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
  const m = Math.floor(s % 3600 / 60), r = s % 60;
  if(d) return h ? `${d} d ${h} h` : `${d} d`;
  if(h) return m ? `${h} h ${m} min` : `${h} h`;
  return r ? `${m}min ${r}s` : `${m}min`;
}

/* ---------- Rótulos do domínio ---------- */
const STATUS_PESSOA = {
  RESTRITO:     {lbl:'Acesso restrito', cls:'c-red',   desc:'Não pode circular — reconhecimento gera ocorrência'},
  FUNCIONARIO:  {lbl:'Funcionário',     cls:'c-blue',  desc:'Colaborador do quadro fixo'},
  TERCEIRIZADO: {lbl:'Terceirizado',    cls:'c-violet',desc:'Prestador com acesso autorizado'},
  VISITANTE:    {lbl:'Visitante',       cls:'c-gray',  desc:'Acesso eventual, sem restrição'}
};
const STATUS_CAMERA = {
  ONLINE:     {lbl:'On-line',     cls:'c-green'},
  OFFLINE:    {lbl:'Offline',     cls:'c-red'},
  MANUTENCAO: {lbl:'Manutenção',  cls:'c-amber'}
};
const STATUS_ALERTA = {
  ABERTO:       {lbl:'Aberto',   cls:'c-red'},
  EM_ANDAMENTO: {lbl:'Em curso', cls:'c-amber'},
  RESOLVIDO:    {lbl:'Resolvido',cls:'c-green'}
};
const ROTULO_OBJ = {
  pessoa:'Pessoa', carro:'Carro', caminhao:'Caminhão', onibus:'Ônibus',
  motocicleta:'Motocicleta', bicicleta:'Bicicleta',
  // Animais pela espécie; `animal` fica para os registros de antes.
  cachorro:'Cachorro', gato:'Gato', passaro:'Pássaro', cavalo:'Cavalo',
  boi:'Boi / vaca', ovelha:'Ovelha', animal:'Animal',
  faca:'Faca / arma branca', tesoura:'Tesoura', bastao:'Bastão / taco',
  celular:'Celular',
  // `volume` é a classe do objeto, não um veredito. Abandono é ação, e
  // só se decide depois de segundos parado — chamar a caixa de "Volume
  // abandonado" afirmava na tela algo que nada havia avaliado, sobre uma
  // scooter estacionada na Portaria.
  mochila:'Mochila', volume:'Volume', capacete:'Capacete',
  placa:'Placa veicular', arma:'Arma de fogo',
  veiculo:'Veículo'   // registros anteriores à separação por tipo
};
/* Espelha `vision.CATALOGO_ACOES`. Quem desenha o seletor por câmera lê o
   catálogo do servidor (/api/info); este mapa serve às telas que só precisam
   do rótulo — filtro de registros, linha do alerta, ficha da detecção. */
const ROTULO_ACAO_UI = {
  corrida:'Corrida súbita', queda:'Queda de pessoa', aglomeracao:'Aglomeração',
  briga:'Possível agressão física', roubo:'Possível roubo',
  permanencia:'Permanência prolongada',
  invasao:'Invasão de perímetro', contrafluxo:'Deslocamento em contrafluxo',
  abandono:'Abandono de objeto', vandalismo:'Dano ao patrimônio', fogo:'Fogo',
  area_restrita:'Pessoa em área restrita',
  fora_de_horario:'Entrada em horário não permitido',
  veiculo_proibido:'Veículo em área proibida',
  arma_detectada:'Detecção de arma', fumaca:'Fumaça'
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
function confirmar(titulo, mensagem, {rotulo = 'Confirmar', perigo = false,
                                      exigeTexto = null, secundario = null} = {}){
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
        <span class="sp" style="flex:1"></span>
        ${secundario ? `<button class="btn btn-soft" id="cfAlt">${esc(secundario)}</button>` : ''}
        <button class="btn ${perigo ? 'btn-danger' : 'btn-primary'}" id="cfSim">${esc(rotulo)}</button>
      </div>`);
    const terminar = v => { fecharModal(); resolve(v); };
    $('#cfNao').onclick = () => terminar(null);
    $('#modalBox').querySelector('.x').onclick = () => terminar(null);
    /* Segunda saída, quando há duas formas legítimas de resolver e uma delas
       não é "cancelar". Devolve o sentinela em vez do texto digitado: quem
       chamou precisa distinguir qual botão foi usado, e o campo de motivo não
       se aplica a este caminho. */
    const alt = $('#cfAlt');
    if(alt) alt.onclick = () => terminar('__secundario__');
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

/* Barras deitadas, para quando as categorias são muitas e têm nome.

   Em pé, dezessete setores viram tiras de dois dígitos de largura com o
   rótulo cortado na diagonal; deitadas, o nome fica numa linha legível e a
   altura cresce com a quantidade em vez de espremer. */
function barrasH(items, opts){
  // viewBox estreito de propósito: o SVG escala pela largura do painel, e
  // num terço de tela um viewBox de 720 encolheria a fonte à metade. Em 360
  // a unidade do desenho equivale quase ao pixel, e o rótulo fica legível.
  const o = Object.assign({w: 360, linha: 20, rotulo: 106, pad: {t: 4, r: 32, b: 4}},
                          opts || {});
  if(!items.length) return `<div class="empty"><p>Sem dados no período.</p></div>`;
  const h = o.pad.t + o.pad.b + items.length * o.linha;
  const iw = o.w - o.rotulo - o.pad.r;
  const max = Math.max(1, ...items.map(i => i.v));
  const alt = Math.min(o.linha - 7, 13);
  // Quantas letras do nome cabem antes da reticência. O padrão serve à coluna
  // estreita do dashboard; quem tem coluna de rótulo mais larga passa a sua.
  const maxNome = o.maxNome || 19;

  const out = items.map((it, i) => {
    const y = o.pad.t + i * o.linha;
    const larg = Math.max(2, (it.v / max) * iw);
    const nome = String(it.k);
    return `<text x="${o.rotulo - 8}" y="${y + alt / 2 + 4}" text-anchor="end" font-size="10.5"
                  fill="var(--txt-2)">${esc(nome.length > maxNome ? nome.slice(0, maxNome - 1) + '…' : nome)}</text>
      <rect x="${o.rotulo}" y="${y}" width="${iw}" height="${alt}" rx="2" fill="var(--ink-700)" opacity=".5"/>
      <rect x="${o.rotulo}" y="${y}" width="${larg.toFixed(1)}" height="${alt}" rx="2"
            fill="${it.cor || 'var(--acc)'}" opacity=".9"/>
      <text x="${(o.rotulo + larg + 7).toFixed(1)}" y="${y + alt / 2 + 4}" font-size="10"
            font-weight="600" font-family="IBM Plex Mono, monospace"
            fill="var(--txt-2)">${it.v}</text>`;
  }).join('');

  return `<svg viewBox="0 0 ${o.w} ${h}" style="width:100%;height:auto;display:block" role="img">${out}</svg>`;
}

/* Percentual que não apaga o que é pequeno.

   Arredondar a uma casa inteira transformava 4 ações em 1.207 detecções num
   "0%" — e 0% é o número que se usa para dizer que não houve nenhuma. Num
   sistema em que a classe rara é justamente a crítica, essa é a leitura que
   não pode sair errada. Abaixo de 1 e acima de zero, usa decimal; abaixo de
   0,1 diz "<0,1%", que é curto e verdadeiro. */
/* Percentuais de uma composição, que somam exatamente 100%.

   Arredondando cada um por conta própria, 994, 1.245 e 4 em 2.404 saíam como
   48%, 52% e 0,2% — 100,2% no rodapé de um sistema cuja razão de existir é a
   conferência. Aqui todos usam uma casa decimal e o resto da divisão vai para
   quem tem a maior sobra (maior resto), que é como se fecha um rateio.

   Zero continua zero: parte sem evento não ganha fatia por arredondamento. */
function donut(parts, opts){
  const o = Object.assign({size:176, thick:24, centro:'', sub:''}, opts || {});
  const S = o.size, R = S/2 - 4, r = R - o.thick, cx = S/2, cy = S/2;
  const tot = parts.reduce((s, p) => s + p.v, 0) || 1;
  /* Fatia com valor mas sem ângulo não é desenhada por nenhum renderizador, e
     some. O piso é de um grau — a menor coisa que o monitor consegue mostrar,
     não uma fatia inflada para parecer maior do que é. O número ao lado
     continua sendo a medida; o arco só serve para ela ser encontrada. */
  const MINIMO = Math.PI / 180;
  let a0 = -Math.PI/2, out = '';
  parts.forEach(p => {
    if(p.v <= 0) return;
    const a1 = a0 + Math.max(MINIMO, (p.v/tot) * Math.PI * 2);
    const grande = (a1 - a0) > Math.PI ? 1 : 0;
    const x0 = cx + R*Math.cos(a0), y0 = cy + R*Math.sin(a0);
    const x1 = cx + R*Math.cos(a1), y1 = cy + R*Math.sin(a1);
    const x2 = cx + r*Math.cos(a1), y2 = cy + r*Math.sin(a1);
    const x3 = cx + r*Math.cos(a0), y3 = cy + r*Math.sin(a0);
    out += `<path d="M${x0.toFixed(2)},${y0.toFixed(2)} A${R},${R} 0 ${grande} 1 ${x1.toFixed(2)},${y1.toFixed(2)} L${x2.toFixed(2)},${y2.toFixed(2)} A${r},${r} 0 ${grande} 0 ${x3.toFixed(2)},${y3.toFixed(2)} Z" fill="${p.cor}"><title>${esc((p.nome ? p.nome + ': ' : '') + fmtNum(p.v) + ' · ' + pctCurto(p.v, tot))}</title></path>`;
    // Fatia miúda ganha um tique para fora do anel: sem ele, um grau de arco
    // se perde na borda e a classe parece ausente.
    if((p.v/tot) * Math.PI * 2 < MINIMO * 3){
      const m = (a0 + a1) / 2;
      out += `<line x1="${(cx + R*Math.cos(m)).toFixed(2)}" y1="${(cy + R*Math.sin(m)).toFixed(2)}"
                    x2="${(cx + (R+5)*Math.cos(m)).toFixed(2)}" y2="${(cy + (R+5)*Math.sin(m)).toFixed(2)}"
                    stroke="${p.cor}" stroke-width="2" stroke-linecap="round"/>`;
    }
    a0 = a1;
  });
  out += `<text x="${cx}" y="${cy-1}" text-anchor="middle" font-size="26" font-weight="600" font-family="IBM Plex Mono, monospace" fill="var(--txt-1)">${esc(o.centro)}</text>`;
  out += `<text x="${cx}" y="${cy+16}" text-anchor="middle" font-size="9.5" letter-spacing="1.2" fill="var(--txt-3)">${esc(o.sub)}</text>`;
  return `<svg viewBox="-6 -6 ${S+12} ${S+12}" style="width:${S}px;max-width:100%;height:auto;display:block" role="img">${out}</svg>`;
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
    carregarFace(c, c.dataset.face);
  });
}

/* Busca o rosto da captura mais recente e o desenha sobre o retrato do
   template.

   O template continua por baixo: quem ainda não foi capturado fica com o
   esquema, que não finge ser fotografia de alguém. A imagem é sempre de uma
   captura — a foto do cadastro é descartada quando o template sai dela. */
function carregarFace(canvas, idBiometrico){
  if(!idBiometrico || !API.access) return;
  const img = new Image();
  img.onload  = () => pintarRecorte(canvas.getContext('2d', {alpha:false}), img,
                                    ...medidaCSS(canvas));
  img.onerror = () => {};   // sem captura guardada: fica o template
  img.src = `${API.base}/pessoas/${encodeURIComponent(idBiometrico)}/face`
          + `?token=${encodeURIComponent(API.access)}`;
}
/* Busca o recorte guardado da captura e o desenha sobre o canvas.

   O desenho sintético continua por baixo: quando não há imagem — detecção
   antiga, ou vinda do simulador — fica o esquema, que não finge ser uma
   fotografia de alguém. */
/* Molda a caixa ao recorte, em vez de moldar o recorte a uma caixa fixa.

   Captura de rosto é vertical; numa moldura 5:3 sobrava preto dos dois lados
   e a imagem ficava perdida no meio. Aqui a moldura assume a proporção do
   recorte, e não sobra nada.

   O teto de ampliação continua valendo: esticar um recorte de 49x85 até a
   largura da coluna não cria detalhe nenhum — vira um borrão que parece
   defeito do sistema. Quando o recorte é pequeno, a moldura fica pequena
   junto, em vez de ampliar até embaçar. */
function encaixarRecorte(canvas, img, larguraMax, limiteAltura){
  const MAX_AMPLIACAO = 3;
  // Sem teto declarado, metade da janela — que serve à ficha, onde a imagem é
  // o assunto. Quem vive dentro de um cartão passa o seu, senão a moldura
  // cresce com o recorte e empurra o próprio cartão.
  const alturaMax = limiteAltura || Math.round(window.innerHeight * 0.5);
  const esc = Math.min(larguraMax / img.naturalWidth,
                       alturaMax / img.naturalHeight,
                       MAX_AMPLIACAO);
  const w = Math.max(1, Math.round(img.naturalWidth * esc));
  const h = Math.max(1, Math.round(img.naturalHeight * esc));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  // Redefinir o tamanho zera a transformação; a escala de DPR entra depois.
  const ctx = canvas.getContext('2d', {alpha:false});
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.drawImage(img, 0, 0, w, h);
}

/* `fonte` diz de onde vem a imagem: 'deteccoes' (o padrão) ou 'alertas'.
   Alerta de sala não nasce de detecção — a saída não fotografa ninguém indo
   embora — e carrega a própria imagem. Sem este parâmetro a coluna CAPTURA
   ficava em branco justamente nos alertas que mais precisam dela. */
function carregarRecorte(canvas, codigo, larguraMax, fonte, alturaMax){
  if(!codigo || !API.access) return;
  const img = new Image();
  img.onload  = () => larguraMax
    ? encaixarRecorte(canvas, img, larguraMax, alturaMax)
    : pintarRecorte(canvas.getContext('2d', {alpha:false}), img, ...medidaCSS(canvas));
  img.onerror = () => {};
  img.src = `${API.base}/${fonte || 'deteccoes'}/${encodeURIComponent(codigo)}/snapshot`
          + `?token=${encodeURIComponent(API.access)}`;
}

/* Moldura neutra enquanto o recorte real não chega — e no lugar dele, quando
   não existe.

   O que havia aqui era uma cena inventada a partir do código da detecção:
   silhueta colorida, caixa, linhas de varredura e vinheta. Ficava por baixo
   da evidência e aparecia sozinha quando ela não existia, sem nada indicar
   que aquele desenho não era a captura. Em sistema de identificação, imagem
   inventada ao lado de registro real é a pior coisa que a tela pode mostrar. */
/* A medida em que o desenho acontece, não a do bitmap.

   `molduraVazia` deixa o contexto escalado pelo DPR, então desenhar usando
   `canvas.width` — que já é a largura multiplicada pelo mesmo DPR — aplica o
   fator duas vezes. Em tela comum o DPR é 1 e nada aparece; num monitor HiDPI
   a imagem saía ao dobro do tamanho e vazava para fora da moldura. */
function medidaCSS(canvas){
  const l = parseFloat(canvas.style.width), a = parseFloat(canvas.style.height);
  return [l || canvas.width, a || canvas.height];
}

function molduraVazia(canvas, largura, altura){
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = largura * dpr; canvas.height = altura * dpr;
  canvas.style.width = largura + 'px'; canvas.style.height = altura + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0A1218';
  ctx.fillRect(0, 0, largura, altura);
}

/* Miniatura da coluna CAPTURA.

   A célula era deitada — 54 por 39 — e a captura de pessoa é em pé: medidos
   400 recortes deste parque, a proporção mediana é 0,73 contra 1,38 da célula.
   A imagem entrava encolhida no meio da moldura, com tarja preta dos dois
   lados, e de um rosto de 54 pixels de largura sobravam 28.

   Aqui a moldura toma a proporção do recorte, como já fazia a do painel de
   alerta: a altura é o teto e a largura segue a imagem, então não há tarja em
   caso nenhum e as linhas da tabela continuam alinhadas pela altura. O teto de
   1,26 vez a largura é onde a captura típica usa a coluna inteira sem esticar
   demais a linha.

   Dava também um defeito que só aparece em tela HiDPI: `pintarRecorte` recebia
   `canvas.width`, que é o bitmap já multiplicado pelo DPR, enquanto o contexto
   vinha escalado pelo mesmo DPR de `molduraVazia` — com dpr 2 a miniatura era
   desenhada ao dobro e vazava para fora da moldura. `encaixarRecorte` dimensiona
   e transforma no mesmo lugar, e não tem como divergir. */
function pintarSnaps(raiz){
  $$('[data-snap]', raiz || document).forEach(c => {
    if(c.dataset.pintado) return;
    let d;
    try{ d = JSON.parse(c.dataset.snap); }catch(e){ return; }
    const w = +(c.getAttribute('width') || 56);
    const h = Math.round(w * 1.26);
    molduraVazia(c, w, h);
    c.dataset.pintado = '1';
    carregarRecorte(c, d.codigo, w, d.fonte, h);
  });
}
/* Só o código: era a cena sintética que precisava de tipo, classe e bbox. */
const snapAttr = (d, fonte) =>
  esc(JSON.stringify(fonte ? {codigo: d.codigo, fonte} : {codigo: d.codigo}));


/* ---------- Veículo: categoria, cor, marca e modelo ---------- */
/* Marca e modelo vêm da placa consultada na base oficial; sem consulta, o
   veículo é "não identificado" — nunca um palpite visual. A cor sai da
   imagem e vem com a fração do veículo que ela ocupa. */
const CATEGORIA_VEICULO = {carro:'Carro', motocicleta:'Motocicleta', caminhao:'Caminhão', onibus:'Ônibus'};
const AMOSTRA_COR = {branco:'#F2F2F2', prata:'#B8BEC6', cinza:'#6E757D', preto:'#15181C',
  vermelho:'#D33A3A', laranja:'#E8782C', amarelo:'#E8C62C', verde:'#3FA34D', azul:'#2F6FD0',
  roxo:'#7E4FC4', marrom:'#7A4E2D', bege:'#D9C7A3'};
function ehVeiculo(d){ return !!(d && CATEGORIA_VEICULO[d.classe]); }
function marcaModelo(d){
  if(!d || !d.veiculo_marca) return 'Veículo não identificado';
  return [d.veiculo_marca, d.veiculo_modelo].filter(Boolean).join(' ')
       + (d.veiculo_ano ? ` · ${d.veiculo_ano}` : '');
}
function corVeiculoHTML(d){
  if(!d || !d.veiculo_cor) return '<span style="color:var(--txt-4)">—</span>';
  const amostra = AMOSTRA_COR[d.veiculo_cor] || '#888';
  const pct = d.veiculo_cor_conf != null ? ` <span class="mono" style="color:var(--txt-4)">${Math.round(d.veiculo_cor_conf*100)}%</span>` : '';
  return `<span style="display:inline-flex;align-items:center;gap:6px">`
       + `<span style="width:11px;height:11px;border-radius:3px;background:${amostra};border:1px solid rgba(255,255,255,.35)"></span>`
       + `${esc(d.veiculo_cor[0].toUpperCase() + d.veiculo_cor.slice(1))}${pct}</span>`;
}
