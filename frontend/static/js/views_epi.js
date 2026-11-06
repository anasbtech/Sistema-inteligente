/* ===================== Conformidade de EPI =====================
   À esquerda, a câmera ao vivo (ou uma imagem enviada) com as caixas de
   cada EPI; no meio, o modelo do corpo com os pontos de cada item e a lista;
   à direita, o resultado da pessoa e os números do dia.

   Cada item tem três estados, e a tela não esconde o terceiro: detectado,
   ausente (o modelo viu a falta) e não verificado (não deu para afirmar —
   botas, por exemplo, não têm detector). "Em conformidade" só com todo item
   exigido detectado. */

const EPI_ROTULO = {capacete:'Capacete', oculos:'Óculos de proteção', mascara:'Máscara',
                    colete:'Colete refletivo', luvas:'Luvas', botas:'Botas de segurança'};
const EPI_ORDEM = ['capacete','oculos','mascara','colete','luvas','botas'];
const EPI_COR = {detectado:'#2FB67C', ausente:'#E8474D', nao_verificado:'#5F7589'};

const EPI = {cam: null, fonte: 'vivo', analise: null, pessoa: 0, timer: null, imagem: null};

/* Quem tem EPI visível primeiro (e, empatado, quem está mais perto): numa
   cena cheia, o trabalhador fica à frente dos passantes ao fundo. */
function epiOrdenar(a){
  if(!a || !a.pessoas) return a;
  const peso = p => Object.values(p.itens).filter(i => i.bbox).length * 1e7
    + (p.pessoa_bbox[2] - p.pessoa_bbox[0]) * (p.pessoa_bbox[3] - p.pessoa_bbox[1]);
  a.pessoas.sort((x, y) => peso(y) - peso(x));
  return a;
}
const EPI_RES_CURTO = {conforme:'Em conformidade', fora:'Falta EPI', inconclusivo:'Inconclusivo', sem_exigencia:'Sem exigência'};

/* O corpo, em pontos: cada região se acende na cor do estado do item. */
function epiCorpoSVG(itens, exigidos){
  const cor = k => {
    const st = (itens[k] || {}).estado || 'nao_verificado';
    return {fill: EPI_COR[st], op: exigidos.includes(k) ? 1 : .35};
  };
  const c = Object.fromEntries(EPI_ORDEM.map(k => [k, cor(k)]));
  const base = '#1F3040', contorno = '#3E5265';
  return `<svg viewBox="0 0 120 300" width="150" height="300" aria-label="Modelo corporal">
    <g stroke="${contorno}" stroke-width="1.2" fill="${base}">
      <circle cx="60" cy="38" r="17"/>
      <rect x="36" y="60" width="48" height="92" rx="12"/>
      <rect x="20" y="64" width="13" height="90" rx="6"/>
      <rect x="87" y="64" width="13" height="90" rx="6"/>
      <rect x="40" y="148" width="17" height="118" rx="7"/>
      <rect x="63" y="148" width="17" height="118" rx="7"/>
    </g>
    <path d="M41 33a19 19 0 0 1 38 0z" fill="${c.capacete.fill}" fill-opacity="${c.capacete.op}" stroke="#0A1218"/>
    <rect x="45" y="35" width="30" height="7" rx="3" fill="${c.oculos.fill}" fill-opacity="${c.oculos.op}" stroke="#0A1218"/>
    <rect x="49" y="45" width="22" height="8" rx="3" fill="${c.mascara.fill}" fill-opacity="${c.mascara.op}" stroke="#0A1218"/>
    <path d="M38 64h44v66H38z" fill="${c.colete.fill}" fill-opacity="${c.colete.op * .85}" stroke="#0A1218"/>
    <path d="M38 96h44M38 112h44" stroke="#E6EEF6" stroke-opacity=".55" stroke-width="2"/>
    <circle cx="26.5" cy="162" r="8" fill="${c.luvas.fill}" fill-opacity="${c.luvas.op}" stroke="#0A1218"/>
    <circle cx="93.5" cy="162" r="8" fill="${c.luvas.fill}" fill-opacity="${c.luvas.op}" stroke="#0A1218"/>
    <rect x="36" y="262" width="23" height="14" rx="4" fill="${c.botas.fill}" fill-opacity="${c.botas.op}" stroke="#0A1218"/>
    <rect x="61" y="262" width="23" height="14" rx="4" fill="${c.botas.fill}" fill-opacity="${c.botas.op}" stroke="#0A1218"/>
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
      <div><b>${esc(EPI_ROTULO[k])}${exigidos.includes(k) ? '' : ' <span style="color:var(--txt-4);font-weight:500">(opcional)</span>'}</b>
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
  $('#epiCorpo').innerHTML = `${epiCorpoSVG(itens, exig)}<div class="epi-lista">${epiListaHTML(itens, exig)}</div>`;
  $('#epiRes').innerHTML = epiResultadoHTML(p);
  $('#epiPessoas').innerHTML = pessoas.length > 1 ? pessoas.map((x, i) =>
    `<button class="btn btn-sm ${i === EPI.pessoa ? 'btn-primary' : 'btn-ghost'}" data-epi-p="${i}">
       Pessoa ${i + 1} · ${EPI_RES_CURTO[x.resultado]}</button>`).slice(0, 8).join('')
       + (pessoas.length > 8 ? `<span class="hint" style="align-self:center">+${pessoas.length - 8} ao fundo</span>` : '') : '';
  $$('[data-epi-p]').forEach(b => b.onclick = () => { EPI.pessoa = +b.dataset.epiP; epiPintarPainel(); epiDesenharImagem(); });
  epiDesenharImagem();
}

/* Imagem enviada: a foto com a caixa de cada pessoa e de cada EPI. */
function epiDesenharImagem(){
  if(EPI.fonte !== 'imagem' || !EPI.imagem || !EPI.analise) return;
  const cv = $('#epiCanvas'); if(!cv) return;
  const img = EPI.imagem, a = EPI.analise;
  cv.width = img.naturalWidth; cv.height = img.naturalHeight;
  const ctx = cv.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const k = Math.max(1, img.naturalWidth / 900);
  const caixa = (b, cor, rot, grosso) => {
    ctx.lineWidth = grosso * k; ctx.strokeStyle = cor;
    ctx.strokeRect(b[0], b[1], b[2] - b[0], b[3] - b[1]);
    ctx.font = `600 ${12 * k}px "IBM Plex Sans",system-ui`;
    const w = ctx.measureText(rot).width + 8 * k;
    ctx.fillStyle = cor; ctx.fillRect(b[0], Math.max(0, b[1] - 16 * k), w, 16 * k);
    ctx.fillStyle = '#08121A'; ctx.fillText(rot, b[0] + 4 * k, Math.max(0, b[1] - 16 * k) + 12 * k);
  };
  a.pessoas.forEach((p, i) => {
    const sel = i === EPI.pessoa;
    caixa(p.pessoa_bbox, sel ? '#35A7DE' : 'rgba(53,167,222,.45)', `Pessoa ${i + 1} ${Math.round(p.pessoa_conf * 100)}%`, sel ? 2.4 : 1.2);
    if(!sel) return;
    EPI_ORDEM.forEach(it => {
      const x = p.itens[it];
      if(x && x.bbox) caixa(x.bbox, EPI_COR[x.estado],
        `${x.estado === 'ausente' ? 'Sem ' + EPI_ROTULO[it].toLowerCase() : EPI_ROTULO[it]} ${Math.round(x.confianca * 100)}%`, 2);
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

async function epiAtualizarVivo(){
  if(APP.view !== 'epi' || EPI.fonte !== 'vivo') return;
  try{
    const r = await API.epiEstado();
    epiPintarDia(r.dia);
    const c = r.cameras.find(x => x.codigo === EPI.cam);
    EPI.analise = epiOrdenar(c && c.ultima ? {...c.ultima, exigidos: c.exigidos} : {pessoas: [], exigidos: c ? c.exigidos : []});
    epiPintarPainel();
    const quando = c && c.ultima ? relTime(c.ultima.em) : 'sem análise ainda';
    $('#epiQuando').textContent = `última análise: ${quando}`;
  }catch(e){ /* a próxima volta tenta de novo */ }
}

async function renderEpi(alvo){
  clearInterval(EPI.timer);
  const r = await API.epiEstado();
  const cams = r.cameras;
  if(!EPI.cam || !cams.some(c => c.codigo === EPI.cam)) EPI.cam = cams.length ? cams[0].codigo : null;
  EPI.fonte = cams.length ? 'vivo' : 'imagem';

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Conformidade de EPI</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="epiCam" ${cams.length ? '' : 'disabled'}>
        ${cams.length ? cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === EPI.cam ? 'selected' : ''}>${esc(c.codigo)} · ${esc(c.nome)}</option>`).join('')
          : '<option>Nenhuma câmera exige EPI</option>'}</select>
      <label class="btn btn-soft btn-sm" style="cursor:pointer">${ico('snap',14)} Analisar imagem
        <input type="file" id="epiArq" accept="image/*" hidden></label>
    </div></div>
  ${r.modelo_disponivel ? '' : `<div class="note" style="margin-bottom:14px">${ico('alert',15)}<div><b>Detector de EPI não instalado.</b> Coloque o modelo em models/epi/ (veja a configuração EPI_MODEL).</div></div>`}
  ${cams.length ? '' : `<div class="note" style="margin-bottom:14px">${ico('info',15)}<div>Nenhuma câmera exige EPI ainda. Em <b>Câmeras › Configurar</b>, marque os EPIs obrigatórios. Enquanto isso, use <b>Analisar imagem</b>.</div></div>`}
  <div class="epi-grade">
    <div class="panel"><div class="panel-hd"><h3 id="epiTitulo">${EPI.fonte === 'vivo' ? 'Câmera ao vivo' : 'Imagem analisada'}</h3>
        <span class="sub" id="epiQuando"></span></div>
      <div class="panel-bd"><div class="epi-palco" id="epiPalco">
        ${EPI.fonte === 'vivo' ? '<canvas id="epiFeed"></canvas>' : `<canvas id="epiCanvas"></canvas>
          <div class="empty" style="position:absolute;inset:0;display:grid;place-items:center;color:var(--txt-3)" id="epiVazio">Envie uma imagem para analisar</div>`}
      </div><div class="epi-pessoas" id="epiPessoas"></div></div></div>
    <div class="panel"><div class="panel-hd"><h3>Modelo corporal (IA)</h3></div>
      <div class="panel-bd"><div class="epi-corpo" id="epiCorpo"></div></div></div>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="panel"><div class="panel-hd"><h3>Resultado da análise</h3></div><div class="panel-bd" id="epiRes"></div></div>
      <div class="panel"><div class="panel-hd"><h3>Estatísticas do dia</h3></div><div class="panel-bd" id="epiDia"></div></div>
    </div>
  </div>`;

  epiPintarDia(r.dia);
  if(EPI.fonte === 'vivo'){
    const cam = camPorCodigo(EPI.cam);
    if(cam) FEED.register($('#epiFeed'), cam, {zonas: false, hd: true, conter: true});
    epiAtualizarVivo();
    EPI.timer = setInterval(epiAtualizarVivo, 3000);
  }else{
    EPI.analise = {pessoas: [], exigidos: []};
    epiPintarPainel();
  }
  $('#epiCam').onchange = e => { EPI.cam = e.target.value; EPI.pessoa = 0; irPara('epi'); };
  $('#epiArq').onchange = async e => {
    const arq = e.target.files[0]; if(!arq) return;
    clearInterval(EPI.timer);
    EPI.fonte = 'imagem';
    $('#epiTitulo').textContent = 'Imagem analisada';
    $('#epiQuando').textContent = 'analisando…';
    $('#epiPalco').innerHTML = '<canvas id="epiCanvas"></canvas>';
    const img = new Image();
    img.src = URL.createObjectURL(arq);
    await img.decode();
    EPI.imagem = img;
    try{
      const cam = cams.find(c => c.codigo === EPI.cam);
      EPI.analise = epiOrdenar(await API.epiAnalisar(arq, cam && cam.exigidos.length ? cam.exigidos : null));
      EPI.pessoa = 0;
      $('#epiQuando').textContent = `${EPI.analise.pessoas.length} pessoa(s) na imagem`;
      epiPintarPainel();
    }catch(err){ erroToast(err); $('#epiQuando').textContent = ''; }
  };
}
