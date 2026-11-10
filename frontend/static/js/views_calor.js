/* ===================== Mapa de calor =====================
   Onde e quando há movimento em cada câmera.

   O mapa é desenhado sobre um quadro atual da própria câmera: a grade de
   contagens que o servidor devolve (96 × 54 células, cada detecção pelo seu
   ponto de apoio) é suavizada com um núcleo gaussiano e colorida numa paleta
   térmica. A escala usa o percentil 99 — com o máximo, um único ponto onde
   alguém ficou parado o dia todo apagaria o resto do mapa.

   Embaixo, a mesma contagem por dia da semana e hora, e a evolução diária;
   ao lado, as câmeras mais movimentadas no período (clique para abrir). */

const CALOR = {cam: null, camada: 'pessoas', rapido: '7d', de: '', ate: '', opac: 0.8,
               imagem: true, dados: null, ranking: null};

const CALOR_RAPIDOS = [['hoje', 'Hoje'], ['7d', '7 dias'], ['30d', '30 dias'], ['tudo', 'Tudo']];
const CALOR_CAMADAS = [['pessoas', 'Pessoas'], ['veiculos', 'Veículos'], ['rostos', 'Rostos']];

/* Paleta térmica: transparente → azul → ciano → verde → amarelo → vermelho. */
const CALOR_PALETA = [
  [0.00, [20, 40, 120, 0]],
  [0.08, [30, 80, 220, 0]],          // abaixo disto, transparente: sem véu azul na cena
  [0.20, [30, 80, 220, 0.42]],
  [0.32, [20, 190, 230, 0.62]],
  [0.52, [60, 210, 90, 0.72]],
  [0.72, [250, 220, 40, 0.82]],
  [0.88, [250, 130, 30, 0.88]],
  [1.00, [235, 40, 40, 0.94]],
];

function calorCor(t, opaco){
  t = Math.max(0, Math.min(1, t));
  let i = 1;
  while(i < CALOR_PALETA.length - 1 && CALOR_PALETA[i][0] < t) i++;
  const [t0, c0] = CALOR_PALETA[i - 1], [t1, c1] = CALOR_PALETA[i];
  const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
  const c = c0.map((v, k) => v + (c1[k] - v) * f);
  return opaco ? [c[0], c[1], c[2], t <= 0 ? 0 : 1] : c;
}
const calorCss = (t) => { const [r, g, b] = calorCor(Math.max(t, 0.06), true); return `rgb(${r|0},${g|0},${b|0})`; };

function calorPeriodo(){
  if(CALOR.rapido === 'custom') return {desde: limiteDia(CALOR.de), ate: limiteDia(CALOR.ate, true)};
  const agora = new Date();
  if(CALOR.rapido === 'tudo') return {desde: new Date(2020, 0, 1).toISOString()};
  if(CALOR.rapido === 'hoje'){
    const h = new Date(agora.getFullYear(), agora.getMonth(), agora.getDate());
    return {desde: h.toISOString()};
  }
  const dias = CALOR.rapido === '30d' ? 30 : 7;
  return {desde: new Date(agora.getTime() - dias * 864e5).toISOString()};
}

/* Suavização gaussiana separável sobre a grade. */
function calorSuavizar(grade, sigma){
  const L = grade.length, C = grade[0].length, r = Math.ceil(sigma * 3);
  const k = []; let soma = 0;
  for(let i = -r; i <= r; i++){ const v = Math.exp(-(i * i) / (2 * sigma * sigma)); k.push(v); soma += v; }
  for(let i = 0; i < k.length; i++) k[i] /= soma;
  const tmp = Array.from({length: L}, () => new Float32Array(C));
  const out = Array.from({length: L}, () => new Float32Array(C));
  for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
    let s = 0; for(let i = -r; i <= r; i++){ const xx = x + i; if(xx >= 0 && xx < C) s += grade[y][xx] * k[i + r]; }
    tmp[y][x] = s;
  }
  for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
    let s = 0; for(let i = -r; i <= r; i++){ const yy = y + i; if(yy >= 0 && yy < L) s += tmp[yy][x] * k[i + r]; }
    out[y][x] = s;
  }
  return out;
}

function calorPercentil(valores, p){
  const v = valores.filter(x => x > 0).sort((a, b) => a - b);
  if(!v.length) return 0;
  return v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))];
}

/* Desenha o mapa no canvas, do tamanho da imagem de fundo. */
function calorDesenhar(){
  const d = CALOR.dados, cv = $('#calorCanvas'), img = $('#calorImg');
  if(!d || !cv) return;
  const palco = $('#calorPalco');
  const W = palco.clientWidth, H = Math.round(W * 9 / 16);
  palco.style.height = H + 'px';
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + 'px'; cv.style.height = H + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if(img) img.style.opacity = CALOR.imagem ? '1' : '0';

  const suave = calorSuavizar(d.grade, 1.7);
  const max = calorPercentil(suave.flat ? suave.flatMap(l => Array.from(l)) : [], 0.995) || 1;
  CALOR.suave = suave; CALOR.max = max;
  const C = d.colunas, L = d.linhas, F = 4;           // ampliação bilinear antes de desenhar
  const pequeno = document.createElement('canvas');
  pequeno.width = C * F; pequeno.height = L * F;
  const pctx = pequeno.getContext('2d'), im = pctx.createImageData(C * F, L * F);
  for(let y = 0; y < L * F; y++){
    const gy = Math.min(L - 1.001, Math.max(0, (y + 0.5) / F - 0.5)), y0 = Math.floor(gy), fy = gy - y0;
    for(let x = 0; x < C * F; x++){
      const gx = Math.min(C - 1.001, Math.max(0, (x + 0.5) / F - 0.5)), x0 = Math.floor(gx), fx = gx - x0;
      const v = suave[y0][x0] * (1 - fx) * (1 - fy) + suave[y0][x0 + 1] * fx * (1 - fy)
              + suave[y0 + 1][x0] * (1 - fx) * fy + suave[y0 + 1][x0 + 1] * fx * fy;
      const t = Math.pow(Math.min(1, v / max), 0.62);
      const [r, g, b, a] = calorCor(t);
      const i = (y * C * F + x) * 4;
      im.data[i] = r; im.data[i + 1] = g; im.data[i + 2] = b; im.data[i + 3] = Math.round(a * 255 * CALOR.opac);
    }
  }
  pctx.putImageData(im, 0, 0);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(pequeno, 0, 0, W, H);

  // Ponto de maior concentração, marcado e nomeado.
  const pico = (d.destaques || {}).area_pico;
  if(pico){
    const px = (pico.coluna + 0.5) / C * W, py = (pico.linha + 0.5) / L * H;
    ctx.strokeStyle = 'rgba(255,255,255,.95)'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(px, py, 14, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(px, py, 17, 0, Math.PI * 2); ctx.stroke();
    const txt = 'PICO';
    ctx.font = '600 11px "IBM Plex Sans", sans-serif';
    const tw = ctx.measureText(txt).width + 10, tx = Math.min(W - tw - 4, px + 20), ty = Math.max(4, py - 26);
    ctx.fillStyle = 'rgba(8,13,19,.85)'; ctx.fillRect(tx, ty, tw, 18);
    ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.fillText(txt, tx + 5, ty + 9);
  }
}

/* Contagem real sob o ponteiro: soma das células num raio de 2. */
function calorSob(ev){
  const d = CALOR.dados, tip = $('#calorTip'), palco = $('#calorPalco');
  if(!d || !tip) return;
  const r = palco.getBoundingClientRect();
  const fx = (ev.clientX - r.left) / r.width, fy = (ev.clientY - r.top) / r.height;
  if(fx < 0 || fy < 0 || fx > 1 || fy > 1){ tip.hidden = true; return; }
  const cx = Math.floor(fx * d.colunas), cy = Math.floor(fy * d.linhas);
  let n = 0;
  for(let y = cy - 2; y <= cy + 2; y++) for(let x = cx - 2; x <= cx + 2; x++)
    if(y >= 0 && x >= 0 && y < d.linhas && x < d.colunas) n += d.grade[y][x];
  const nivel = CALOR.suave ? Math.min(1, CALOR.suave[cy][cx] / (CALOR.max || 1)) : 0;
  tip.hidden = false;
  tip.style.left = Math.min(r.width - 170, ev.clientX - r.left + 14) + 'px';
  tip.style.top = Math.max(0, ev.clientY - r.top - 34) + 'px';
  tip.innerHTML = `<b>${fmtNum(n)}</b> passagem(ns) nesta área<br><span>intensidade ${Math.round(nivel * 100)}%</span>`;
}

function calorSemanaHTML(d){
  const max = Math.max(1, ...d.semana.flat());
  const pico = (d.destaques || {}).pico_semana;
  const horas = Array.from({length: 24}, (_, h) => h);
  return `<div class="calor-semana">
    <div class="cs-canto"></div>
    ${horas.map(h => `<div class="cs-hora">${h % 3 === 0 ? String(h).padStart(2, '0') : ''}</div>`).join('')}
    ${d.semana.map((linha, i) => `<div class="cs-dia">${esc(d.dias[i])}</div>` + linha.map((n, h) => {
      const t = n / max, ehPico = pico && pico.dia === d.dias[i] && pico.hora === h && n > 0;
      return `<div class="cs-cel${ehPico ? ' pico' : ''}" style="background:${n ? calorCss(Math.pow(t, 0.7)) : 'var(--ink-800)'}"
        title="${esc(d.dias[i])} ${String(h).padStart(2, '0')}h — ${fmtNum(n)} passagem(ns)"></div>`;
    }).join('')).join('')}
  </div>
  <div class="calor-legenda"><span>menos</span><i style="background:linear-gradient(90deg,${[0.06, 0.3, 0.5, 0.7, 0.88, 1].map(calorCss).join(',')})"></i><span>mais</span></div>`;
}

function calorDiasHTML(d){
  if(!d.por_dia.length) return vazio('heat', 'Sem movimento no período.', '');
  const max = Math.max(1, ...d.por_dia.map(x => x.total));
  return `<div class="calor-dias">${d.por_dia.map(x => `
    <div class="cd-col" title="${fmtDia(x.dia)} — ${fmtNum(x.total)} passagem(ns)">
      <div class="cd-barra" style="height:${Math.max(3, x.total / max * 100)}%;background:${calorCss(x.total / max)}"></div>
      <span>${x.dia.slice(8, 10)}/${x.dia.slice(5, 7)}</span></div>`).join('')}</div>`;
}

function calorRankingHTML(r){
  const cams = (r && r.cameras) || [];
  if(!cams.length) return vazio('heat', 'Nenhuma câmera com movimento nesta camada.', '');
  const max = Math.max(1, cams[0].total);
  return `<div class="calor-rank">${cams.slice(0, 14).map((c, i) => `
    <button class="cr-item ${c.codigo === CALOR.cam ? 'on' : ''}" data-cam="${esc(c.codigo)}">
      <span class="cr-pos">${i + 1}</span>
      <span class="cr-nome"><b>${esc(c.nome)}</b><small>${esc(c.codigo)}${c.setor ? ' · ' + esc(c.setor) : ''}</small>
        <span class="cr-barra"><i style="width:${c.total / max * 100}%;background:${calorCss(c.total / max)}"></i></span></span>
      <span class="cr-n">${fmtNum(c.total)}</span>
    </button>`).join('')}</div>`;
}

function calorKpis(d){
  const x = d.destaques || {};
  const h2 = h => `${String(h).padStart(2, '0')}h–${String((h + 1) % 24).padStart(2, '0')}h`;
  const kpi = (cls, lbl, icone, val, sub) => `<div class="kpi ${cls}"><div class="kpi-top"><span class="kpi-lbl">${lbl}</span>
    <span class="kpi-ico">${ico(icone, 15)}</span></div><div class="kpi-val">${val}</div><div class="kpi-delta nt">${sub}</div></div>`;
  return `<div class="calor-kpis">
    ${kpi('', 'Passagens', 'heat', fmtNum(x.total || 0), CALOR_CAMADAS.find(c => c[0] === CALOR.camada)[1].toLowerCase() + ' no período')}
    ${kpi('k-violet', 'Média por dia', 'gauge', x.media_diaria != null ? fmtNum(x.media_diaria) : '—', 'no período escolhido')}
    ${kpi('k-amber', 'Hora de pico', 'clock', x.hora_pico ? h2(x.hora_pico.hora) : '—', x.hora_pico ? `${fmtNum(x.hora_pico.total)} passagens somadas` : 'sem movimento')}
    ${kpi('k-red', 'Pico da semana', 'alert', x.pico_semana ? `${esc(x.pico_semana.dia)} ${String(x.pico_semana.hora).padStart(2, '0')}h` : '—', x.dia_pico ? `dia mais movimentado: ${esc(x.dia_pico.dia)}` : '—')}
    ${kpi('k-green', 'Maior concentração', 'pin', x.area_pico ? '' : '—', x.area_pico ? esc(x.area_pico.regiao) : 'sem movimento')}
  </div>`;
}

async function calorCarregar(){
  const per = calorPeriodo();
  const alvo = $('#calorCorpo');
  if(!alvo) return;
  try{
    const [d, r] = await Promise.all([
      API.calorMapa({camera: CALOR.cam, camada: CALOR.camada, ...per}),
      API.calorRanking({camada: CALOR.camada, ...per})]);
    CALOR.dados = d; CALOR.ranking = r;
    $('#calorKpis').innerHTML = calorKpis(d);
    $('#calorRank').innerHTML = calorRankingHTML(r);
    $('#calorSemana').innerHTML = calorSemanaHTML(d);
    $('#calorDias').innerHTML = calorDiasHTML(d);
    $('#calorSub').textContent = `${d.camera.nome} · ${fmtDT(d.desde)} → ${fmtDT(d.ate)}`
      + (d.destaques.sem_posicao ? ` · ${fmtNum(d.destaques.sem_posicao)} sem posição na imagem` : '');
    $('#calorVazio').hidden = (d.destaques.total || 0) > 0;
    $$('.cr-item', $('#calorRank')).forEach(b => b.onclick = () => { CALOR.cam = b.dataset.cam; renderMapaCalor($('#v-heatmap')); });
    calorDesenhar();
  }catch(e){ erroToast(e); }
}

async function renderMapaCalor(alvo){
  const per = calorPeriodo();
  const r = await API.calorRanking({camada: CALOR.camada, ...per});
  const cams = APP.cameras && APP.cameras.length ? APP.cameras : await API.cameras();
  if(!CALOR.cam || !cams.some(c => c.codigo === CALOR.cam))
    CALOR.cam = ((r.cameras || [])[0] || cams[0] || {}).codigo || null;
  const cam = cams.find(c => c.codigo === CALOR.cam);
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Mapa de calor</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="calorCam">${cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === CALOR.cam ? 'selected' : ''}>
        ${esc(c.codigo)} · ${esc(c.nome)}</option>`).join('')}</select>
      <div class="segm" id="calorCamada">${CALOR_CAMADAS.map(([k, v]) => `<button data-k="${k}" class="${CALOR.camada === k ? 'on' : ''}">${v}</button>`).join('')}</div>
    </div></div>
  <div class="calor-filtros">
    <div class="segm" id="calorRapido">${CALOR_RAPIDOS.map(([k, v]) => `<button data-k="${k}" class="${CALOR.rapido === k ? 'on' : ''}">${v}</button>`).join('')}</div>
    <div class="periodo">
      <label for="calorDe">Período</label>
      <input class="inp" id="calorDe" type="date" value="${esc(CALOR.de)}" max="${diaISO(new Date())}">
      <span class="sep">até</span>
      <input class="inp" id="calorAte" type="date" value="${esc(CALOR.ate)}" max="${diaISO(new Date())}">
    </div>
  </div>
  <div id="calorKpis"></div>
  <div class="calor-grade" id="calorCorpo">
    <div class="panel"><div class="panel-hd"><h3>Onde há movimento — ${esc(cam ? cam.nome : '')}</h3><span class="sub" id="calorSub"></span>
        <span class="sp"></span>
        <label class="calor-ctl">Intensidade <input type="range" id="calorOpac" min="0.3" max="1" step="0.05" value="${CALOR.opac}"></label>
        <label class="calor-ctl"><input type="checkbox" id="calorImgOn" ${CALOR.imagem ? 'checked' : ''}> imagem da câmera</label></div>
      <div class="panel-bd">
        <div class="calor-palco" id="calorPalco">
          ${cam ? `<img id="calorImg" alt="" src="${API.base}/stream/${encodeURIComponent(cam.codigo)}/snapshot?token=${encodeURIComponent(API.access)}"
               onerror="this.remove()">` : ''}
          <canvas id="calorCanvas"></canvas>
          <div class="calor-tip" id="calorTip" hidden></div>
          <div class="calor-vazio" id="calorVazio" hidden>${vazio('heat', 'Sem movimento nesta câmera no período.', 'Escolha outro período ou outra camada.')}</div>
        </div>
        <div class="calor-legenda"><span>pouco movimento</span>
          <i style="background:linear-gradient(90deg,${[0.06, 0.3, 0.5, 0.7, 0.88, 1].map(calorCss).join(',')})"></i><span>muito movimento</span></div>
        <p class="hint" style="margin:8px 0 0">Cada ${CALOR.camada === 'rostos' ? 'rosto entra pelo centro da captura' : 'detecção entra pelo ponto em que toca o chão'};
          o mapa é suavizado e a escala vai do mínimo ao percentil 99,5, para um ponto isolado não apagar o resto. Passe o mouse para ver a contagem.</p>
      </div></div>
    <div class="panel"><div class="panel-hd"><h3>Câmeras mais movimentadas</h3><span class="sub">clique para abrir</span></div>
      <div class="panel-bd" id="calorRank">${calorRankingHTML(r)}</div></div>
  </div>
  <div class="calor-grade2">
    <div class="panel"><div class="panel-hd"><h3>Quando — dia da semana × hora</h3></div>
      <div class="panel-bd" id="calorSemana">${carregando()}</div></div>
    <div class="panel"><div class="panel-hd"><h3>Movimento por dia</h3></div>
      <div class="panel-bd" id="calorDias">${carregando()}</div></div>
  </div>`;

  $('#calorCam').onchange = e => { CALOR.cam = e.target.value; renderMapaCalor(alvo); };
  $$('#calorCamada button').forEach(b => b.onclick = () => { CALOR.camada = b.dataset.k; renderMapaCalor(alvo); });
  $$('#calorRapido button').forEach(b => b.onclick = () => {
    CALOR.rapido = b.dataset.k; CALOR.de = ''; CALOR.ate = '';
    $$('#calorRapido button').forEach(x => x.classList.toggle('on', x === b));
    $('#calorDe').value = ''; $('#calorAte').value = '';
    calorCarregar();
  });
  const datas = () => {
    CALOR.de = $('#calorDe').value; CALOR.ate = $('#calorAte').value;
    if(!CALOR.de) return;
    CALOR.rapido = 'custom';
    $$('#calorRapido button').forEach(x => x.classList.remove('on'));
    calorCarregar();
  };
  $('#calorDe').onchange = datas; $('#calorAte').onchange = datas;
  $('#calorOpac').oninput = e => { CALOR.opac = +e.target.value; calorDesenhar(); };
  $('#calorImgOn').onchange = e => { CALOR.imagem = e.target.checked; calorDesenhar(); };
  const palco = $('#calorPalco');
  palco.onmousemove = calorSob;
  palco.onmouseleave = () => { $('#calorTip').hidden = true; };
  const img = $('#calorImg');
  if(img) img.onload = calorDesenhar;
  if(CALOR._redim) window.removeEventListener('resize', CALOR._redim);
  CALOR._redim = () => { if(APP.view === 'heatmap') calorDesenhar(); };
  window.addEventListener('resize', CALOR._redim);
  await calorCarregar();
}
