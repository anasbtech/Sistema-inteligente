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
   Cada equipamento é um grupo próprio: detectado aparece inteiro com a
   cantoneira verde; ausente aparece apagado com a cantoneira vermelha; não
   verificado, com a cantoneira cinza tracejada. Item não exigido fica
   desenhado, sem marcação. */
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
  const op = k => exigidos.includes(k) && st(k) === 'ausente' ? .16 : 1;
  const cantos = ([x1, y1, x2, y2], cor, tracejado) => {
    const d = 9, a = `stroke="${cor}" stroke-width="2.4" fill="none" stroke-linecap="round"${tracejado ? ' stroke-dasharray="3 3"' : ''}`;
    return `<path ${a} d="M${x1} ${y1 + d}V${y1}H${x1 + d}M${x2 - d} ${y1}H${x2}V${y1 + d}M${x2} ${y2 - d}V${y2}H${x2 - d}M${x1 + d} ${y2}H${x1}V${y2 - d}"/>`;
  };
  const marcas = EPI_ORDEM.filter(k => exigidos.includes(k)).map(k => {
    const e = st(k), cor = EPI_COR[e], i = itens[k] || {};
    const caixas = EPI_AREA[k].map(b => cantos(b, cor, e !== 'detectado')).join('');
    const [x1, y1] = EPI_AREA[k][0];
    const txt = `${EPI_CURTO[k]}${i.confianca != null ? ' ' + Math.round(i.confianca * 100) + '%' : e === 'ausente' ? ' ✕' : ' ?'}`;
    const w = txt.length * 6.4 + 10;
    return `${caixas}<g><rect x="${x1}" y="${y1 - 15}" width="${w}" height="14" rx="2.5" fill="${cor}"/>
      <text x="${x1 + 5}" y="${y1 - 4.5}" font-size="10.5" font-weight="700" fill="#08121A"
            font-family="IBM Plex Sans,system-ui">${esc(txt)}</text></g>`;
  }).join('');
  return `<svg viewBox="0 0 240 470" width="100%" style="max-width:330px;min-height:430px" aria-label="Trabalhador com EPI">
  <defs>
    <linearGradient id="eSk" x1="0" x2="1"><stop offset="0" stop-color="#B9805C"/><stop offset=".5" stop-color="#D49A74"/><stop offset="1" stop-color="#A9704F"/></linearGradient>
    <linearGradient id="eNv" x1="0" x2="1"><stop offset="0" stop-color="#1A2A44"/><stop offset=".5" stop-color="#26395A"/><stop offset="1" stop-color="#16243A"/></linearGradient>
    <linearGradient id="eVe" x1="0" x2="1"><stop offset="0" stop-color="#B9D80F"/><stop offset=".5" stop-color="#DDF52A"/><stop offset="1" stop-color="#AECB0C"/></linearGradient>
    <linearGradient id="eHe" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#C9D1D9"/></linearGradient>
    <linearGradient id="eRf" x1="0" x2="1"><stop offset="0" stop-color="#AEB7C0"/><stop offset=".5" stop-color="#F2F5F8"/><stop offset="1" stop-color="#9BA5AF"/></linearGradient>
    <radialGradient id="eFu" cx=".5" cy=".45" r=".6"><stop offset="0" stop-color="#1B2A38"/><stop offset="1" stop-color="#0D161F" stop-opacity="0"/></radialGradient>
  </defs>
  <ellipse cx="120" cy="230" rx="118" ry="230" fill="url(#eFu)"/>
  <ellipse cx="120" cy="452" rx="62" ry="7" fill="#000" opacity=".35"/>
  <!-- pernas e calça com faixas refletivas -->
  <path d="M90 262 L84 404 H112 L118 272 Z" fill="url(#eNv)"/>
  <path d="M150 262 L156 404 H128 L122 272 Z" fill="url(#eNv)"/>
  <path d="M85 360h30M126 360h30" stroke="url(#eRf)" stroke-width="6"/>
  <!-- botas -->
  <g opacity="${op('botas')}">
    <path d="M80 398h36v30q0 6-6 6H76q-8 0-8-7v-6q0-6 7-8z" fill="#2B2420"/>
    <path d="M124 398h36q5 2 5 8v6q0 7-8 7h-27q-6 0-6-6z" fill="#2B2420"/>
    <path d="M68 432h48v6H70q-2 0-2-2zM124 432h48v4q0 2-2 2h-46z" fill="#E0A21B"/>
    <path d="M84 404h28M128 404h28" stroke="#4A3F38" stroke-width="2"/>
  </g>
  <!-- tronco e braços (camisa) -->
  <path d="M74 122 Q120 108 166 122 L170 182 L156 274 H84 L70 182 Z" fill="url(#eNv)"/>
  <path d="M74 124 Q58 132 54 160 L46 256 H66 L76 172 Z" fill="url(#eNv)"/>
  <path d="M166 124 Q182 132 186 160 L194 256 H174 L164 172 Z" fill="url(#eNv)"/>
  <!-- luvas -->
  <g opacity="${op('luvas')}">
    <path d="M44 252h24l2 26q0 14-12 15q-14 0-15-13z" fill="#1E1F22"/>
    <path d="M172 252h24l-1 28q-1 13-15 13q-12-1-12-15z" fill="#1E1F22"/>
    <path d="M44 258h24M172 258h24" stroke="#F0A02A" stroke-width="3"/>
  </g>
  <!-- colete refletivo -->
  <g opacity="${op('colete')}">
    <path d="M78 126 L106 120 L120 172 L134 120 L162 126 L166 186 L152 268 H88 L74 186 Z" fill="url(#eVe)"/>
    <path d="M76 204h88M79 232h82" stroke="url(#eRf)" stroke-width="9"/>
    <path d="M96 124v80M144 124v80" stroke="url(#eRf)" stroke-width="7" opacity=".9"/>
    <path d="M120 172v96" stroke="#9DB80B" stroke-width="1.5"/>
  </g>
  <!-- pescoço e cabeça -->
  <path d="M108 96h24v26q-12 6-24 0z" fill="url(#eSk)"/>
  <ellipse cx="94" cy="72" rx="5" ry="9" fill="#B07552"/>
  <ellipse cx="146" cy="72" rx="5" ry="9" fill="#B07552"/>
  <path d="M95 60 Q95 34 120 34 Q145 34 145 60 L144 86 Q140 106 120 108 Q100 106 96 86 Z" fill="url(#eSk)"/>
  <path d="M108 92q12 5 24 0" stroke="#7A4A33" stroke-width="1.6" fill="none"/>
  <!-- máscara -->
  <g opacity="${op('mascara')}">
    <path d="M100 80 Q120 74 140 80 L138 98 Q120 110 102 98 Z" fill="#D9E6F2"/>
    <path d="M104 86h32M105 92h30" stroke="#AFC2D3" stroke-width="1.2"/>
    <path d="M100 82 L94 74M140 82 L146 74" stroke="#D9E6F2" stroke-width="1.6"/>
  </g>
  <!-- óculos -->
  <g opacity="${op('oculos')}">
    <path d="M93 64 Q120 58 147 64" stroke="#1B2A38" stroke-width="3" fill="none"/>
    <rect x="98" y="60" width="20" height="13" rx="5" fill="#9FD3F0" fill-opacity=".55" stroke="#1B2A38" stroke-width="2"/>
    <rect x="122" y="60" width="20" height="13" rx="5" fill="#9FD3F0" fill-opacity=".55" stroke="#1B2A38" stroke-width="2"/>
    <path d="M102 63l6 0M126 63l6 0" stroke="#fff" stroke-width="1.4" opacity=".8"/>
  </g>
  <!-- capacete -->
  <g opacity="${op('capacete')}">
    <path d="M92 54 Q92 26 120 24 Q148 26 148 54 Z" fill="url(#eHe)"/>
    <path d="M86 54 H154 Q156 60 150 60 H90 Q84 60 86 54 Z" fill="#E4E9EE"/>
    <path d="M120 24 V54" stroke="#C3CBD3" stroke-width="5"/>
    <path d="M100 52 Q100 34 112 29" stroke="#fff" stroke-width="2" fill="none" opacity=".9"/>
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
    const pct = i.confianca != null ? ` · ${Math.round(i.confianca * 100)}%` : '';
    return `<div class="epi-item ${cls} ${exigidos.includes(k) ? '' : 'opc'}">
      <span class="bola">${simb}</span>
      <div><b>${esc(EPI_ROTULO[k])}${exigidos.includes(k) ? '' : ' <span style="color:var(--txt-4);font-weight:500">(não exigido)</span>'}</b>
        <small>${txt}${pct}${k === 'botas' ? ' — sem detector' : ''}</small></div></div>`;
  }).join('');
}

function epiResultadoHTML(p){
  if(!p) return `<div class="epi-res vazio"><div class="ico-g">–</div><h3>Ninguém em cena</h3>
    <p>Aguardando uma pessoa na câmera.</p></div>`;
  const m = {
    conforme: ['✓', 'Em conformidade', 'Todos os EPIs exigidos foram identificados.'],
    fora: ['!', 'Fora de conformidade', `Falta: ${p.faltando.map(f => EPI_ROTULO[f].toLowerCase()).join(', ')}.`],
    inconclusivo: ['?', 'Inconclusivo', 'Algum EPI exigido não pôde ser verificado nesta imagem.'],
    sem_exigencia: ['–', 'Sem EPI exigido', 'Esta câmera não exige EPI.'],
  }[p.resultado];
  return `<div class="epi-res ${p.resultado}"><div class="ico-g">${m[0]}</div><h3>${m[1]}</h3><p>${esc(m[2])}</p></div>`;
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
  $('#epiPessoas').innerHTML = pessoas.length > 1 ? pessoas.map((x, i) =>
    `<button class="btn btn-sm ${i === EPI.pessoa ? 'btn-primary' : 'btn-ghost'}" data-epi-p="${i}">
       Pessoa ${i + 1} · ${EPI_RES_CURTO[x.resultado]}</button>`).slice(0, 8).join('')
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
      if(x && x.bbox) caixa(x.bbox, EPI_COR[x.estado],
        `${x.estado === 'ausente' ? 'Sem ' + EPI_CURTO[it].toLowerCase() : EPI_CURTO[it]} ${Math.round(x.confianca * 100)}%`, 2);
    });
  });
}

function epiPintarDia(d){
  const pct = d.conformidade == null ? '—' : `${(d.conformidade * 100).toFixed(1)}%`;
  const faltas = Object.entries(d.faltando || {}).sort((a, b) => b[1] - a[1]);
  $('#epiDia').innerHTML = `
    <div class="epi-stat"><span>Pessoas analisadas hoje</span><b>${fmtNum(d.analisadas)}</b></div>
    <div class="epi-stat"><span>Conformidade</span><b style="color:var(--green)">${pct}</b></div>
    <div class="epi-stat"><span>Fora de conformidade</span><b style="color:var(--red)">${fmtNum(d.fora)}</b></div>
    <div class="epi-stat"><span>Inconclusivas</span><b>${fmtNum(d.inconclusivo)}</b></div>
    <div class="epi-stat"><span>Alertas de EPI hoje</span><b>${fmtNum(d.alertas)}</b></div>
    ${faltas.length ? `<div class="hint" style="margin:10px 0 4px">EPI que mais falta</div>
      ${faltas.slice(0, 4).map(([k, n]) => `<div class="epi-stat"><span>${esc(EPI_ROTULO[k] || k)}</span><b>${fmtNum(n)}</b></div>`).join('')}` : ''}`;
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

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Conformidade de EPI</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="epiCam">${cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === EPI.cam ? 'selected' : ''}>
        ${esc(c.codigo)} · ${esc(c.nome)}${comEpi.has(c.codigo) ? ' · exige EPI' : ''}</option>`).join('')}</select>
      <label class="btn btn-ghost btn-sm" style="cursor:pointer" title="Analisar uma foto em vez da câmera">${ico('snap',14)} Testar com foto
        <input type="file" id="epiArq" accept="image/*" hidden></label>
    </div></div>
  ${r.modelo_disponivel ? '' : `<div class="note" style="margin-bottom:14px">${ico('alert',15)}<div><b>Detector de EPI não instalado.</b> Coloque o modelo em models/epi/ (veja a configuração EPI_MODEL).</div></div>`}
  ${exig && exig.length ? '' : `<div class="note" style="margin-bottom:14px">${ico('info',15)}<div>Esta câmera não tem EPI obrigatório configurado: a análise usa capacete, colete, óculos e luvas. Para valer alerta, marque os EPIs em <b>Câmeras › Configurar</b>.</div></div>`}
  <div class="epi-grade">
    <div class="panel"><div class="panel-hd"><h3 id="epiTitulo">${camSel ? `Câmera ao vivo — ${esc(camSel.nome)}` : 'Câmera ao vivo'}</h3>
        <span class="sub" id="epiQuando">analisando…</span></div>
      <div class="panel-bd"><div class="epi-palco" id="epiPalco">
        <canvas id="epiFeed"></canvas><canvas id="epiSobre"></canvas>
      </div><div class="epi-pessoas" id="epiPessoas"></div></div></div>
    <div class="panel"><div class="panel-hd"><h3>Modelo corporal (IA)</h3>
        <span class="sub">pessoa selecionada</span></div>
      <div class="panel-bd"><div class="epi-corpo" id="epiCorpo"></div></div></div>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="panel"><div class="panel-hd"><h3>Resultado da análise</h3></div><div class="panel-bd" id="epiRes"></div></div>
      <div class="panel"><div class="panel-hd"><h3>Estatísticas do dia</h3></div><div class="panel-bd" id="epiDia"></div></div>
    </div>
  </div>`;

  epiPintarDia(r.dia);
  EPI.analise = {pessoas: [], exigidos: exig && exig.length ? exig : EPI_PADRAO};
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
    try{
      EPI.analise = epiOrdenar(await API.epiAnalisar(arq, exig && exig.length ? exig : EPI_PADRAO));
      EPI.pessoa = 0;
      $('#epiQuando').textContent = `${EPI.analise.pessoas.length} pessoa(s) na foto · escolha outra câmera para voltar ao vivo`;
      epiPintarPainel();
    }catch(err){ erroToast(err); $('#epiQuando').textContent = ''; }
  };
}
