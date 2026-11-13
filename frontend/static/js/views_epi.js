/* ===================== Conformidade de EPI =====================
   À esquerda, a câmera ao vivo: o quadro atual é analisado a cada poucos
   segundos e as caixas de cada pessoa e de cada EPI são desenhadas sobre o
   vídeo. No meio, o trabalhador ilustrado com cada equipamento marcado; à
   direita, o resultado e os números do dia.

   Cada item tem três estados, e a tela não esconde o terceiro: detectado,
   ausente (o modelo viu a falta) e não verificado (não deu para afirmar —
   botas, por exemplo, não têm detector). "Em conformidade" só com todo item
   exigido detectado. */

const EPI_ROTULO = {capacete:'Capacete', oculos:'Óculos de proteção', mascara:'Máscara',
                    colete:'Colete refletivo', luvas:'Luvas', botas:'Botas de segurança'};
const EPI_CURTO = {capacete:'Capacete', oculos:'Óculos', mascara:'Máscara',
                   colete:'Colete', luvas:'Luvas', botas:'Botas'};
const EPI_ORDEM = ['capacete','oculos','mascara','colete','luvas','botas'];
const EPI_COR = {detectado:'#2FB67C', ausente:'#E8474D', nao_verificado:'#8A9BAC'};
const EPI_RES_CURTO = {conforme:'Em conformidade', fora:'Falta EPI', inconclusivo:'Inconclusivo', sem_exigencia:'Sem exigência'};
const EPI_PADRAO = ['capacete','colete','oculos','luvas'];

const EPI = {cam: null, fonte: 'camera', analise: null, pessoa: 0, timer: null,
             imagem: null, ocupado: false};

/* Quem tem EPI visível primeiro (e, empatado, quem está mais perto). */
function epiOrdenar(a){
  if(!a || !a.pessoas) return a;
  const peso = p => Object.values(p.itens).filter(i => i.bbox).length * 1e7
    + (p.pessoa_bbox[2] - p.pessoa_bbox[0]) * (p.pessoa_bbox[3] - p.pessoa_bbox[1]);
  a.pessoas.sort((x, y) => peso(y) - peso(x));
  return a;
}

/* ---------- O trabalhador ilustrado ----------
   O boneco está sempre vestido com o EPI completo — é a referência do que
   se espera. O que a IA viu muda a cor (capacete e colete na cor da
   câmera) e a marcação: cantoneira verde = detectado; vermelha, com o item
   apagado = ausente; cinza tracejada = não verificado. As etiquetas ficam
   nas laterais, alinhadas, ligadas ao item por uma linha. */
const EPI_AREA = {
  capacete: [[86, 22, 154, 60]],
  oculos:   [[90, 58, 150, 76]],
  mascara:  [[98, 78, 142, 106]],
  colete:   [[72, 120, 168, 270]],
  luvas:    [[38, 252, 74, 296], [166, 252, 202, 296]],
  botas:    [[72, 396, 118, 446], [122, 396, 168, 446]],
};
function epiTrabalhadorSVG(itens, exigidos){
  const st = k => (itens[k] || {}).estado || 'nao_verificado';
  const op = k => st(k) === 'ausente' ? .28 : 1;
  // Cor vista pela câmera (capacete, colete), clareada e escurecida para o volume.
  const tom = (hex, f) => '#' + [1, 3, 5].map(i => Math.max(0, Math.min(255,
    Math.round(parseInt(hex.slice(i, i + 2), 16) * f))).toString(16).padStart(2, '0')).join('');
  const corCap = (itens.capacete || {}).estado === 'detectado' && itens.capacete.cor || '#F2C230';
  const corCol = (itens.colete || {}).estado === 'detectado' && itens.colete.cor || '#D2EC1E';
  const cantos = ([x1, y1, x2, y2], cor, tracejado) => {
    const d = 9, a = `stroke="${cor}" stroke-width="2.4" fill="none" stroke-linecap="round"${tracejado ? ' stroke-dasharray="3 3"' : ''}`;
    return `<path ${a} d="M${x1} ${y1 + d}V${y1}H${x1 + d}M${x2 - d} ${y1}H${x2}V${y1 + d}M${x2} ${y2 - d}V${y2}H${x2 - d}M${x1 + d} ${y2}H${x1}V${y2 - d}"/>`;
  };
  // Etiqueta: lado (e/d), altura e qual caixa do item ela aponta.
  const ETIQ = {capacete: ['d', 34, 0], oculos: ['e', 67, 0], mascara: ['d', 94, 0],
                colete: ['e', 160, 0], luvas: ['d', 274, 1], botas: ['e', 420, 0]};
  const marcas = EPI_ORDEM.filter(k => exigidos.includes(k) || st(k) !== 'nao_verificado').map(k => {
    const e = st(k), cor = EPI_COR[e], i = itens[k] || {};
    const caixas = EPI_AREA[k].map(b => cantos(b, cor, e !== 'detectado')).join('');
    const [lado, y, alvo] = ETIQ[k];
    const [bx1, by1, bx2, by2] = EPI_AREA[k][alvo];
    // Só o resultado: o nome do item está na lista ao lado do boneco.
    const txt = e === 'ausente' ? '✕' : i.confianca != null ? Math.round(i.confianca * 100) + '%' : '?';
    const w = Math.max(30, txt.length * 10 + 14);
    const ly = Math.max(by1 + 3, Math.min(by2 - 3, y));
    const [rx, lx1, lx2] = lado === 'e' ? [50 - w, 50, bx1] : [210, 210, bx2];
    return `${caixas}<g>
      <path d="M${lx1} ${y}H${(lx1 + lx2) / 2}L${lx2} ${ly}" stroke="${cor}" stroke-width="1.2" fill="none" opacity=".85"/>
      <circle cx="${lx2}" cy="${ly}" r="2.2" fill="${cor}"/>
      <rect x="${rx}" y="${y - 12}" width="${w}" height="24" rx="4" fill="${cor}"/>
      <text x="${rx + w / 2}" y="${y + 5.5}" text-anchor="middle" font-size="16" font-weight="700" fill="#08121A"
            font-family="IBM Plex Sans,system-ui">${esc(txt)}</text></g>`;
  }).join('');
  return `<svg viewBox="-12 4 278 462" width="100%" style="max-width:440px;max-height:440px" aria-label="Trabalhador com EPI">
  <defs>
    <linearGradient id="eSk" x1="0" x2="1"><stop offset="0" stop-color="#A86E4C"/><stop offset=".45" stop-color="#D39C78"/><stop offset="1" stop-color="#9A6343"/></linearGradient>
    <linearGradient id="eNv" x1="0" x2="1"><stop offset="0" stop-color="#16233A"/><stop offset=".5" stop-color="#2A3E62"/><stop offset="1" stop-color="#121D31"/></linearGradient>
    <linearGradient id="eNv2" x1="0" x2="1"><stop offset="0" stop-color="#121D31"/><stop offset=".6" stop-color="#24365A"/><stop offset="1" stop-color="#101A2C"/></linearGradient>
    <linearGradient id="eVe" x1="0" x2="1"><stop offset="0" stop-color="${tom(corCol, .78)}"/><stop offset=".5" stop-color="${tom(corCol, 1.08)}"/><stop offset="1" stop-color="${tom(corCol, .74)}"/></linearGradient>
    <linearGradient id="eHe" x1="0" x2="1"><stop offset="0" stop-color="${tom(corCap, .78)}"/><stop offset=".45" stop-color="${tom(corCap, 1.12)}"/><stop offset="1" stop-color="${tom(corCap, .72)}"/></linearGradient>
    <linearGradient id="eRf" x1="0" x2="1"><stop offset="0" stop-color="#9EA8B3"/><stop offset=".5" stop-color="#F4F7FA"/><stop offset="1" stop-color="#8E98A3"/></linearGradient>
    <linearGradient id="eLe" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#CFEFFF" stop-opacity=".85"/><stop offset="1" stop-color="#5FA8D3" stop-opacity=".55"/></linearGradient>
    <linearGradient id="eLu" x1="0" x2="1"><stop offset="0" stop-color="#CFC8B6"/><stop offset=".5" stop-color="#F1EDE2"/><stop offset="1" stop-color="#C4BCA8"/></linearGradient>
    <linearGradient id="eBo" x1="0" x2="1"><stop offset="0" stop-color="#2A211C"/><stop offset=".5" stop-color="#4A3B31"/><stop offset="1" stop-color="#231B17"/></linearGradient>
    <radialGradient id="eFu" cx=".5" cy=".42" r=".62"><stop offset="0" stop-color="#1A2A3A"/><stop offset="1" stop-color="#0D161F" stop-opacity="0"/></radialGradient>
  </defs>
  <ellipse cx="120" cy="230" rx="122" ry="232" fill="url(#eFu)"/>
  <ellipse cx="120" cy="447" rx="66" ry="7" fill="#000" opacity=".4"/>

  <!-- pernas do macacão -->
  <path d="M81 268 L84 402 H115 L120 284 L125 402 H156 L159 268 Z" fill="url(#eNv)"/>
  <path d="M120 284 V402" stroke="#0E1828" stroke-width="2"/>
  <path d="M86 356 H114 M126 356 H154" stroke="url(#eRf)" stroke-width="6"/>
  <path d="M88 312 q12 4 24 0 M128 312 q12 4 24 0" stroke="#0E1828" stroke-width="1.4" fill="none" opacity=".7"/>

  <!-- botas de segurança -->
  <g opacity="${op('botas')}">
    <g id="eBota">
      <path d="M85 396 H115 V424 L118 430 Q120 438 112 440 H76 Q68 440 69 433 Q70 425 80 422 L85 420 Z" fill="url(#eBo)"/>
      <path d="M70 432 Q72 426 80 424 L92 424 Q86 430 86 440 H76 Q68 440 70 432 Z" fill="#5A4A3E" opacity=".55"/>
      <path d="M67 438 H119 V443 Q119 447 115 447 H71 Q67 447 67 443 Z" fill="#E0A21B"/>
      <path d="M85 402 H115" stroke="#6A5748" stroke-width="2"/>
      <path d="M92 408 h16 M92 413 h16" stroke="#8A7564" stroke-width="1.3"/>
    </g>
    <use href="#eBota" transform="translate(240 0) scale(-1 1)"/>
  </g>

  <!-- tronco e braços -->
  <path d="M76 128 Q88 116 106 115 L120 122 L134 115 Q152 116 164 128 L170 188 L160 272 H80 L70 188 Z" fill="url(#eNv)"/>
  <path d="M77 128 Q63 133 58 150 L50 248 H70 L76 182 Z" fill="url(#eNv2)"/>
  <path d="M163 128 Q177 133 182 150 L190 248 H170 L164 182 Z" fill="url(#eNv2)" transform="translate(0 0)"/>
  <path d="M51 238 H69 V248 H50 Z M171 238 H189 L190 248 H171 Z" fill="#0F192A"/>
  <path d="M60 196 q6 2 12 0 M168 196 q6 2 12 0" stroke="#0E1828" stroke-width="1.3" fill="none" opacity=".7"/>
  <!-- gola -->
  <path d="M106 115 L120 134 L134 115 L128 113 L120 124 L112 113 Z" fill="#33496F"/>
  <!-- cinto -->
  <path d="M80 262 H160 V272 H80 Z" fill="#0C1422"/>
  <rect x="113" y="261" width="14" height="12" rx="2" fill="#7F8A96" stroke="#4E5864" stroke-width="1"/>

  <!-- luvas -->
  <g opacity="${op('luvas')}">
    <g id="eLuva">
      <path d="M48 248 H70 L72 260 H46 Z" fill="#E2A12A"/>
      <path d="M46 258 H72 L73 280 Q73 294 60 295 Q46 295 44 282 Z" fill="url(#eLu)" stroke="#A99F89" stroke-width="1"/>
      <path d="M72 266 Q79 268 78 276 Q77 282 72 282" fill="url(#eLu)" stroke="#A99F89" stroke-width="1"/>
      <path d="M52 284 V292 M58 285 V294 M64 284 V292" stroke="#B3A98F" stroke-width="1.2" stroke-linecap="round"/>
    </g>
    <use href="#eLuva" transform="translate(240 0) scale(-1 1)"/>
  </g>

  <!-- colete refletivo -->
  <g opacity="${op('colete')}">
    <path d="M80 129 L106 119 L120 166 L134 119 L160 129 L166 190 L156 266 H84 L74 190 Z" fill="url(#eVe)"/>
    <path d="M77 204 H163 L162 213 H78 Z M79 232 H161 L160 241 H80 Z" fill="url(#eRf)"/>
    <path d="M95 123 L99 125 V204 H92 V126 Z M145 123 L141 125 V204 H148 V126 Z" fill="url(#eRf)" opacity=".95"/>
    <path d="M120 166 V266" stroke="${tom(corCol, .6)}" stroke-width="1.6" stroke-dasharray="3 2"/>
    <path d="M100 248 h14 v12 h-14 z M126 248 h14 v12 h-14 z" fill="none" stroke="${tom(corCol, .65)}" stroke-width="1.2"/>
  </g>

  <!-- pescoço e cabeça -->
  <path d="M109 98 H131 V118 Q120 126 109 118 Z" fill="url(#eSk)"/>
  <path d="M109 110 Q120 116 131 110" stroke="#8A5638" stroke-width="1.2" fill="none" opacity=".5"/>
  <ellipse cx="96" cy="72" rx="5" ry="9" fill="#9C6545"/>
  <ellipse cx="144" cy="72" rx="5" ry="9" fill="#9C6545"/>
  <path d="M97 62 C97 42 107 33 120 33 C133 33 143 42 143 62 L142 84 C140 99 131 107 120 107 C109 107 100 99 98 84 Z" fill="url(#eSk)"/>
  <path d="M104 52 q6 -3 12 -1 M124 51 q6 -2 12 1" stroke="#5A3A28" stroke-width="2" fill="none" stroke-linecap="round"/>
  <path d="M120 72 q-2 8 0 10" stroke="#8A5638" stroke-width="1.3" fill="none" opacity=".7"/>

  <!-- máscara -->
  <g opacity="${op('mascara')}">
    <path d="M100 79 C108 76 132 76 140 79 L139 95 C134 103 127 106 120 106 C113 106 106 103 101 95 Z" fill="#EAF1F7"/>
    <path d="M100 79 L95 72 M140 79 L145 72 M101 95 L96 80 M139 95 L144 80" stroke="#D3DEE8" stroke-width="1.4" fill="none"/>
    <path d="M103 86 H137 M104 92 H136 M107 98 H133" stroke="#B9CAD9" stroke-width="1.1"/>
    <path d="M110 79 H130" stroke="#9AAAB8" stroke-width="2" stroke-linecap="round"/>
  </g>

  <!-- óculos de proteção -->
  <g opacity="${op('oculos')}">
    <path d="M94 66 H146" stroke="#17222E" stroke-width="3"/>
    <path d="M99 61 H141 Q146 61 146 66 V69 Q146 76 139 76 H128 Q124 76 122.5 72.5 Q120 70 117.5 72.5 Q116 76 112 76 H101 Q94 76 94 69 V66 Q94 61 99 61 Z"
          fill="url(#eLe)" stroke="#17222E" stroke-width="1.8"/>
    <path d="M100 64 L110 64 M128 64 L136 64" stroke="#fff" stroke-width="1.4" stroke-linecap="round" opacity=".85"/>
  </g>

  <!-- capacete -->
  <g opacity="${op('capacete')}">
    <path d="M92 56 C92 35 104 23 120 23 C136 23 148 35 148 56 Z" fill="url(#eHe)"/>
    <path d="M114 24 Q120 23 126 24 L127 55 H113 Z" fill="${tom(corCap, 1.18)}" opacity=".9"/>
    <path d="M84 54 Q84 51 88 51 H152 Q156 51 156 54 L155 58 Q155 61 151 61 H89 Q85 61 85 58 Z" fill="${tom(corCap, .9)}"/>
    <path d="M88 58 H152" stroke="${tom(corCap, .62)}" stroke-width="1.4"/>
    <path d="M100 50 C100 37 106 30 112 28" stroke="#fff" stroke-width="2.2" fill="none" stroke-linecap="round" opacity=".75"/>
    <circle cx="132" cy="42" r="4.5" fill="${tom(corCap, .7)}" opacity=".6"/>
  </g>
  ${marcas}
  </svg>`;
}

function epiListaHTML(itens, exigidos){
  return EPI_ORDEM.map(k => {
    const i = itens[k] || {estado: 'nao_verificado'};
    const cls = {detectado: 'ok', ausente: 'falta', nao_verificado: 'nv'}[i.estado];
    const txt = {detectado: 'Detectado', ausente: 'Ausente', nao_verificado: 'Não verificado'}[i.estado];
    const simb = {detectado: '✓', ausente: '✕', nao_verificado: '?'}[i.estado];
    const exigido = exigidos.includes(k);
    return `<div class="epi-item ${cls} ${exigido ? '' : 'opc'}" title="${esc(EPI_ROTULO[k])}">
      <span class="bola">${simb}</span>
      <div><b>${esc(EPI_CURTO[k])}</b><small>${exigido ? 'exigido' : 'não exigido'}</small></div>
      <em>${txt}${i.confianca != null ? ` <span class="mono">${Math.round(i.confianca * 100)}%</span>` : ''}</em></div>`;
  }).join('');
}

function epiResultadoHTML(p){
  if(!p) return `<div class="epi-res vazio"><div class="ico-g">${ico('users', 18)}</div>
    <div><h3>Ninguém em cena</h3><p>Aguardando uma pessoa na câmera.</p></div></div>`;
  const m = {
    conforme: ['✓', 'Em conformidade', 'Todos os EPIs exigidos foram identificados.'],
    fora: ['!', 'Fora de conformidade', `Falta: ${p.faltando.map(f => EPI_ROTULO[f].toLowerCase()).join(', ')}.`],
    inconclusivo: ['?', 'Inconclusivo', 'Algum EPI exigido não pôde ser verificado nesta imagem.'],
    sem_exigencia: ['–', 'Sem EPI exigido', 'Esta câmera não exige EPI.'],
  }[p.resultado];
  return `<div class="epi-res ${p.resultado}"><div class="ico-g">${m[0]}</div>
    <div><h3>${m[1]}</h3><p>${esc(m[2])}</p></div>
    <span class="epi-res-conf mono">pessoa ${Math.round((p.pessoa_conf || 0) * 100)}%</span></div>`;
}

function epiPintarPainel(){
  const a = EPI.analise;
  const pessoas = (a && a.pessoas) || [];
  if(EPI.pessoa >= pessoas.length) EPI.pessoa = 0;
  const p = pessoas[EPI.pessoa];
  const exig = (a && a.exigidos) || [];
  const itens = p ? p.itens : {};
  $('#epiCorpo').innerHTML = `<div class="epi-boneco">${epiTrabalhadorSVG(itens, exig)}</div>
    <div class="epi-lista">${epiListaHTML(itens, exig)}</div>`;
  $('#epiRes').innerHTML = epiResultadoHTML(p);
  const sub = $('#epiPessoaSub');
  if(sub) sub.textContent = pessoas.length ? `pessoa ${EPI.pessoa + 1} de ${pessoas.length}` : 'nenhuma pessoa';
  const COR = {conforme: 'var(--green)', fora: 'var(--red)', inconclusivo: 'var(--txt-3)', sem_exigencia: 'var(--txt-4)'};
  $('#epiPessoas').innerHTML = pessoas.length > 1 ? `<span class="epi-pessoas-lbl">Pessoas na cena</span>
    <div class="segm">${pessoas.slice(0, 8).map((x, i) =>
      `<button class="${i === EPI.pessoa ? 'on' : ''}" data-epi-p="${i}" title="${esc(EPI_RES_CURTO[x.resultado])}">
         <i class="epi-pdot" style="background:${COR[x.resultado]}"></i>Pessoa ${i + 1}</button>`).join('')}</div>`
       + (pessoas.length > 8 ? `<span class="hint" style="align-self:center">+${pessoas.length - 8} ao fundo</span>` : '') : '';
  $$('[data-epi-p]').forEach(b => b.onclick = () => { EPI.pessoa = +b.dataset.epiP; epiPintarPainel(); });
  epiDesenharCaixas();
}

/* As caixas sobre o vídeo (ou sobre a foto de teste), na mesma área em que
   o quadro foi pintado — o vídeo é desenhado inteiro, com faixas pretas. */
function epiDesenharCaixas(){
  const cv = $('#epiSobre'); if(!cv) return;
  const a = EPI.analise;
  const r = cv.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = r.width * dpr; cv.height = r.height * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, r.width, r.height);
  if(EPI.fonte === 'foto' && EPI.imagem){
    const e = Math.min(r.width / EPI.imagem.naturalWidth, r.height / EPI.imagem.naturalHeight);
    const w = EPI.imagem.naturalWidth * e, h = EPI.imagem.naturalHeight * e;
    ctx.drawImage(EPI.imagem, (r.width - w) / 2, (r.height - h) / 2, w, h);
  }
  if(!a || !a.largura || !a.pessoas) return;
  const e = Math.min(r.width / a.largura, r.height / a.altura);
  const ox = (r.width - a.largura * e) / 2, oy = (r.height - a.altura * e) / 2;
  const T = b => [ox + b[0] * e, oy + b[1] * e, ox + b[2] * e, oy + b[3] * e];
  const caixa = (b, cor, rot, grosso) => {
    const [x1, y1, x2, y2] = T(b);
    ctx.lineWidth = grosso + 2; ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.lineWidth = grosso; ctx.strokeStyle = cor; ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.font = '600 11px "IBM Plex Sans",system-ui';
    const w = ctx.measureText(rot).width + 8, ty = Math.max(0, y1 - 15);
    ctx.fillStyle = cor; ctx.fillRect(x1, ty, w, 15);
    ctx.fillStyle = '#08121A'; ctx.fillText(rot, x1 + 4, ty + 11);
  };
  a.pessoas.forEach((p, i) => {
    const sel = i === EPI.pessoa;
    caixa(p.pessoa_bbox, sel ? '#35A7DE' : 'rgba(53,167,222,.5)',
          `Pessoa ${i + 1} ${Math.round(p.pessoa_conf * 100)}%`, sel ? 2.2 : 1.2);
    if(!sel) return;               // EPI só da pessoa selecionada: a cena não vira um borrão
    EPI_ORDEM.forEach(it => {
      const x = p.itens[it];
      if(!x || !x.bbox) return;
      const rot = `${x.estado === 'ausente' ? 'Sem ' + EPI_CURTO[it].toLowerCase() : EPI_CURTO[it]} ${Math.round(x.confianca * 100)}%`;
      (x.caixas || [x.bbox]).forEach(b => caixa(b, EPI_COR[x.estado], rot, 2));   // luvas: uma caixa por mão
    });
  });
}

function epiPintarDia(d){
  const tot = d.analisadas || 0;
  const k = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
    <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
    <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
  const pct = d.conformidade == null ? '—' : `${(d.conformidade * 100).toFixed(1).replace('.', ',')}<small>%</small>`;
  $('#epiKpis').innerHTML = `
    ${k('', 'Pessoas analisadas hoje', 'users', fmtNum(tot), 'nas câmeras com análise de EPI', dashBarra(tot ? 1 : 0, 'var(--acc)'))}
    ${k('k-green', 'Conformidade', 'check', pct, 'com todo EPI exigido visível', dashBarra(d.conformidade || 0, 'var(--green)'))}
    ${k('k-red', 'Fora de conformidade', 'alert', fmtNum(d.fora || 0), `${fmtNum(d.alertas || 0)} alerta(s) de EPI hoje`, dashBarra(tot ? (d.fora || 0) / tot : 0, 'var(--red)'))}
    ${k('k-amber', 'Inconclusivas', 'eye', fmtNum(d.inconclusivo || 0), 'EPI exigido não verificável na imagem', dashBarra(tot ? (d.inconclusivo || 0) / tot : 0, 'var(--amber)'))}`;
  const faltas = Object.entries(d.faltando || {}).sort((a, b) => b[1] - a[1]);
  const max = faltas.length ? faltas[0][1] : 0, soma = faltas.reduce((s, [, n]) => s + n, 0);
  $('#epiFaltas').innerHTML = faltas.length ? `<div class="ac-lista">${faltas.map(([kk, n], i) => `
      <div class="ac-tipo" style="--sev:var(--red);cursor:default">
        <span class="ob-pos">${pad(i + 1)}</span>
        <span class="ac-nome">${esc(EPI_ROTULO[kk] || kk)}</span>
        <span class="ac-sev">faltou</span>
        <span class="ob-barra"><i style="width:${(n / max * 100).toFixed(1)}%;background:var(--red)"></i></span>
        <b class="mono">${fmtNum(n)}</b><em class="mono">${pctCurto(n, soma)}</em>
      </div>`).join('')}</div>`
    : vazio('check', 'Nenhuma falta de EPI registrada hoje.');
}

/* Análise do quadro atual da câmera, a cada ~3 s enquanto a tela está aberta. */
async function epiAnalisarCamera(){
  if(APP.view !== 'epi' || EPI.fonte !== 'camera' || !EPI.cam || EPI.ocupado) return;
  EPI.ocupado = true;
  try{
    const r = await API.epiCamera(EPI.cam);
    if(APP.view !== 'epi' || EPI.fonte !== 'camera' || r.camera !== EPI.cam) return;
    EPI.analise = epiOrdenar(r);
    $('#epiQuando').textContent = `analisado ${fmtTime(r.em)} · ${r.pessoas.length} pessoa(s)`;
    epiPintarPainel();
  }catch(e){
    $('#epiQuando').textContent = e && e.message ? e.message : 'sem imagem';
  }finally{ EPI.ocupado = false; }
}

async function renderEpi(alvo){
  clearInterval(EPI.timer);
  const [r] = await Promise.all([API.epiEstado()]);
  const cams = (APP.cameras || []).filter(c => c.status === 'ONLINE' && c.rtsp_url !== '');
  const comEpi = new Set(r.cameras.map(c => c.codigo));
  if(!EPI.cam || !cams.some(c => c.codigo === EPI.cam))
    EPI.cam = (r.cameras[0] || cams[0] || {}).codigo || null;
  EPI.fonte = 'camera'; EPI.analise = null; EPI.pessoa = 0;
  const camSel = cams.find(c => c.codigo === EPI.cam);
  const exig = (r.cameras.find(c => c.codigo === EPI.cam) || {}).exigidos;
  const usados = exig && exig.length ? exig : EPI_PADRAO;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Conformidade de EPI</h2><span class="cm-sub">equipamentos de proteção por pessoa e câmera</span><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="epiCam">${cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === EPI.cam ? 'selected' : ''}>
        ${esc(c.codigo)} · ${esc(c.nome)}${comEpi.has(c.codigo) ? ' · exige EPI' : ''}</option>`).join('')}</select>
      <label class="btn btn-ghost btn-sm" style="cursor:pointer" title="Analisar uma foto em vez da câmera">${ico('snap',14)} Testar com foto
        <input type="file" id="epiArq" accept="image/*" hidden></label>
    </div></div>
  <div class="dash-grid dash-kpis" id="epiKpis"></div>
  ${r.modelo_disponivel ? '' : `<div class="note" style="margin-bottom:14px">${ico('alert',15)}<div><b>Detector de EPI não instalado.</b> Coloque o modelo em models/epi/ (veja a configuração EPI_MODEL).</div></div>`}

  <div class="epi-grade">
    <div class="panel"><div class="panel-hd"><h3 id="epiTitulo">${camSel ? esc(camSel.nome) : 'Câmera ao vivo'}</h3>
        <span class="sub mono">${camSel ? esc(camSel.codigo) : ''}</span><span class="sp"></span>
        <span class="epi-vivo" id="epiQuando">analisando…</span></div>
      <div class="panel-bd"><div class="epi-palco" id="epiPalco">
        <canvas id="epiFeed"></canvas><canvas id="epiSobre"></canvas>
      </div>
      <div class="epi-exige"><span>${exig && exig.length ? 'Exigidos nesta câmera' : 'Sem EPI obrigatório — analisando o padrão'}</span>
        ${usados.map(x => `<em>${esc(EPI_CURTO[x] || x)}</em>`).join('')}
        ${exig && exig.length ? '' : `<span class="epi-exige-dica" title="Para valer alerta, marque os EPIs em Câmeras › Configurar.">${ico('info',13)}</span>`}</div>
      <div class="epi-pessoas" id="epiPessoas"></div></div></div>

    <div class="panel"><div class="panel-hd"><h3>Análise da pessoa</h3>
        <span class="sub" id="epiPessoaSub">nenhuma pessoa</span></div>
      <div class="panel-bd"><div id="epiRes"></div><div class="epi-corpo" id="epiCorpo"></div></div></div>
  </div>

  <div class="panel" style="margin-top:14px"><div class="panel-hd"><h3>EPI que mais falta hoje</h3>
      <span class="sub">pessoas fora de conformidade, por item</span></div>
    <div class="panel-bd" id="epiFaltas"></div></div>`;

  epiPintarDia(r.dia);
  EPI.analise = {pessoas: [], exigidos: usados};
  epiPintarPainel();
  if(camSel){
    FEED.register($('#epiFeed'), camSel, {zonas: false, boxes: false, hd: true, conter: true});
    FEED.start();
    epiAnalisarCamera();
    EPI.timer = setInterval(epiAnalisarCamera, 3000);
  }
  $('#epiCam').onchange = e => { EPI.cam = e.target.value; irPara('epi'); };
  $('#epiArq').onchange = async e => {
    const arq = e.target.files[0]; if(!arq) return;
    clearInterval(EPI.timer);
    EPI.fonte = 'foto';
    $('#epiTitulo').textContent = 'Foto de teste';
    $('#epiQuando').textContent = 'analisando…';
    $('#epiFeed').style.display = 'none';
    const img = new Image();
    img.src = URL.createObjectURL(arq);
    await img.decode();
    EPI.imagem = img;
    EPI.analise = null;
    epiDesenharCaixas();              // a foto aparece já, as caixas quando a análise voltar
    try{
      EPI.analise = epiOrdenar(await API.epiAnalisar(arq, usados));
      EPI.pessoa = 0;
      $('#epiQuando').textContent = `${EPI.analise.pessoas.length} pessoa(s) na foto · escolha outra câmera para voltar ao vivo`;
      epiPintarPainel();
    }catch(err){ erroToast(err); $('#epiQuando').textContent = ''; }
  };
}
