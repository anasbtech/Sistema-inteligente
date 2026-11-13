/* ===================== Mapa de calor — análise histórica =====================
   O mesmo módulo, olhando para trás: onde as pessoas ficaram, quando, quantas
   ao mesmo tempo e onde se juntaram, num período escolhido, com a variação
   sobre o período anterior de mesma duração.

   Tudo vem do servidor (/mapa-calor/analise e /mapa-calor/cameras):
   * Ocupação — segundos de presença por ponto do chão, somados a cada volta
     da análise (desde que o acumulador passou a gravar);
   * Passagens — o ponto em que cada passagem registrada apareceu (todo o
     histórico do registro).
   Nenhuma pessoa é identificada nesta tela. */

const CA = {periodo: '24h', base: 'auto', comparar: true, desde: '', ate: '', dados: null, cams: null,
            camadas: {focos: true, grupos: true, zonas: false}, req: 0};

const CA_PERIODOS = [['1h', 'Última hora'], ['hoje', 'Hoje'], ['24h', '24 horas'], ['7d', '7 dias'],
                     ['30d', '30 dias'], ['custom', 'Período']];

function caIntervalo(){
  const agora = new Date();
  const h = n => new Date(agora.getTime() - n * 3600e3);
  switch(CA.periodo){
    case '1h': return [h(1), agora];
    case 'hoje': { const d = new Date(agora); d.setHours(0, 0, 0, 0); return [d, agora]; }
    case '7d': return [h(24 * 7), agora];
    case '30d': return [h(24 * 30), agora];
    case 'custom': {
      const d = CA.desde ? new Date(CA.desde + 'T00:00:00') : h(24 * 7);
      const a = CA.ate ? new Date(CA.ate + 'T23:59:59') : agora;
      return [d, a > agora ? agora : a];
    }
    default: return [h(24), agora];
  }
}

const caHoras = s => { s = Number(s || 0);
  if(s < 60) return `${Math.round(s)} s`;
  if(s < 3600) return `${Math.round(s / 60)} min`;
  const h = s / 3600; return `${h < 10 ? h.toFixed(1).replace('.', ',') : fmtNum(Math.round(h))} h`; };
const caVar = (a, b) => (b == null || !isFinite(b) || b === 0) ? null : (a - b) / b;
function caDelta(v, inverso){
  if(v == null) return '<span class="ca-d nt">sem base anterior</span>';
  const p = Math.round(v * 100), cls = p === 0 ? 'nt' : ((p > 0) !== !!inverso ? 'up' : 'dn');
  return `<span class="ca-d ${cls}">${p > 0 ? '▲' : p < 0 ? '▼' : '='} ${Math.abs(p)}% <em>vs anterior</em></span>`;
}
const caRotBalde = (iso, grao) => { const d = new Date(iso);
  return grao === 'hora' ? `${pad(d.getHours())}h` : `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`; };

/* ---------- Mapa sobre o quadro da câmera ---------- */
function caPintarMapa(){
  const d = CA.dados, palco = $('#caPalco'), cv = $('#caCanvas');
  if(!d || !palco || !cv) return;
  const W = palco.clientWidth, H = palco.clientHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); cv.style.width = W + 'px'; cv.style.height = H + 'px';
  const o = cv.getContext('2d'); o.setTransform(dpr, 0, 0, dpr, 0, 0); o.clearRect(0, 0, W, H);
  const {colunas: C, linhas: L, valores} = d.grade;
  const lut = calorLut();
  if(valores && valores.length){
    // Densidade: a grade é suavizada aqui (gaussiana separável, numa grade
    // 4× mais fina) antes de virar cor. Desfocar a imagem pronta espalhava a
    // transparência e o calor sumia; suavizar o valor faz cada ponto virar
    // uma mancha contínua, com o centro quente e a borda fria.
    const F = 4, CW = C * F, CH = L * F, base = new Float32Array(CW * CH);
    for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
      const v = (valores[y][x] || 0) / 1000;
      if(v > 0) base[(y * F + (F >> 1)) * CW + x * F + (F >> 1)] = v;
    }
    const sigma = 2.2 * F, r = Math.ceil(sigma * 3), ker = new Float32Array(2 * r + 1);
    for(let i = -r; i <= r; i++) ker[i + r] = Math.exp(-(i * i) / (2 * sigma * sigma));
    const tmp = new Float32Array(CW * CH), den = new Float32Array(CW * CH);
    for(let y = 0; y < CH; y++) for(let x = 0; x < CW; x++){
      const v = base[y * CW + x]; if(!v) continue;
      for(let i = -r; i <= r; i++){ const xx = x + i; if(xx >= 0 && xx < CW) tmp[y * CW + xx] += v * ker[i + r]; }
    }
    let mx = 0;
    for(let y = 0; y < CH; y++) for(let x = 0; x < CW; x++){
      const v = tmp[y * CW + x]; if(!v) continue;
      for(let i = -r; i <= r; i++){ const yy = y + i; if(yy >= 0 && yy < CH){ const k = yy * CW + x; den[k] += v * ker[i + r]; } }
    }
    for(let i = 0; i < den.length; i++) if(den[i] > mx) mx = den[i];
    const g = calorCanvas('caGrade', CW, CH), gc = g.getContext('2d'), im = gc.createImageData(CW, CH);
    for(let i = 0; i < den.length; i++){
      const v = mx ? den[i] / mx : 0, p = i * 4;
      if(v < 0.02){ im.data[p + 3] = 0; continue; }
      // Raiz: o foco principal não apaga a circulação menor ao redor.
      const s = Math.sqrt(v), t = Math.min(1, 0.12 + 0.88 * s), k = Math.round(t * 255) * 3;
      im.data[p] = lut[k]; im.data[p + 1] = lut[k + 1]; im.data[p + 2] = lut[k + 2];
      im.data[p + 3] = Math.round(255 * Math.min(0.88, 0.15 + 0.95 * s));
    }
    gc.putImageData(im, 0, 0);
    o.save(); o.imageSmoothingEnabled = true; o.imageSmoothingQuality = 'high';
    o.filter = 'blur(2px)';
    o.drawImage(g, 0, 0, W, H);
    o.restore();
  }
  // Zonas A1…D6.
  if(CA.camadas.zonas){
    const [ZL, ZC] = d.zonas_dim;
    o.strokeStyle = 'rgba(255,255,255,.22)'; o.lineWidth = 1; o.setLineDash([3, 4]);
    for(let i = 1; i < ZC; i++){ const x = Math.round(W * i / ZC) + .5; o.beginPath(); o.moveTo(x, 0); o.lineTo(x, H); o.stroke(); }
    for(let i = 1; i < ZL; i++){ const y = Math.round(H * i / ZL) + .5; o.beginPath(); o.moveTo(0, y); o.lineTo(W, y); o.stroke(); }
    o.setLineDash([]); o.font = '600 10px "IBM Plex Mono", monospace'; o.fillStyle = 'rgba(255,255,255,.75)';
    d.zonas.forEach((p, i) => { const zl = (i / ZC) | 0, zc = i % ZC;
      o.fillText(`${'ABCD'[zl]}${zc + 1} · ${Math.round(p * 100)}%`, W * zc / ZC + 6, H * zl / ZL + 14); });
  }
  // Aglomerações: anel vermelho pulsante onde houve grupo.
  if(CA.camadas.grupos) for(const f of d.focos_grupo || []){
    const x = f.x * W, y = f.y * H, r = Math.max(18, f.raio * W);
    o.strokeStyle = 'rgba(255,72,72,.95)'; o.lineWidth = 2; o.setLineDash([5, 4]);
    o.beginPath(); o.arc(x, y, r, 0, Math.PI * 2); o.stroke(); o.setLineDash([]);
    o.fillStyle = 'rgba(255,72,72,.14)'; o.fill();
  }
  // Focos numerados.
  if(CA.camadas.focos) (d.focos || []).forEach((f, i) => {
    const x = f.x * W, y = f.y * H;
    o.shadowColor = 'rgba(0,0,0,.7)'; o.shadowBlur = 4;
    o.fillStyle = 'rgba(6,10,20,.85)'; o.beginPath(); o.arc(x, y, 11, 0, Math.PI * 2); o.fill();
    o.shadowBlur = 0; o.strokeStyle = '#fff'; o.lineWidth = 1.5; o.stroke();
    o.fillStyle = '#fff'; o.font = '700 11px "IBM Plex Mono", monospace'; o.textAlign = 'center'; o.textBaseline = 'middle';
    o.fillText(String(i + 1), x, y + .5);
  });
  o.textAlign = 'start'; o.textBaseline = 'alphabetic';
}

/* Dica sobre o mapa: a zona e a fatia dela. */
function caDica(ev){
  const d = CA.dados, palco = $('#caPalco'), tip = $('#caTip');
  if(!d || !palco || !tip) return;
  const r = palco.getBoundingClientRect(), fx = (ev.clientX - r.left) / r.width, fy = (ev.clientY - r.top) / r.height;
  if(fx < 0 || fy < 0 || fx > 1 || fy > 1){ tip.hidden = true; return; }
  const [ZL, ZC] = d.zonas_dim, zl = Math.min(ZL - 1, (fy * ZL) | 0), zc = Math.min(ZC - 1, (fx * ZC) | 0), i = zl * ZC + zc;
  const p = d.zonas[i] || 0, ant = d.anterior && d.anterior.zonas ? d.anterior.zonas[i] : null;
  const mx = Math.max(...d.zonas) || 1;
  tip.hidden = false;
  tip.style.left = Math.min(r.width - 200, ev.clientX - r.left + 14) + 'px';
  tip.style.top = Math.min(r.height - 90, ev.clientY - r.top + 14) + 'px';
  tip.innerHTML = `<div class="ct-z">Zona ${'ABCD'[zl]}${zc + 1}</div>
    <div class="ct-n"><b>${(p * 100).toFixed(1).replace('.', ',')}%</b> ${d.base === 'ocupacao' ? 'do tempo de presença' : 'das passagens'}</div>
    <div class="ct-b"><i style="width:${p / mx * 100}%;background:${calorTom(0.35 + 0.6 * p / mx)}"></i></div>
    <div class="ct-s">${ant == null ? 'sem período anterior para comparar' : `anterior: ${(ant * 100).toFixed(1).replace('.', ',')}% (${p - ant >= 0 ? '+' : ''}${((p - ant) * 100).toFixed(1).replace('.', ',')} p.p.)`}</div>`;
}

/* ---------- Gráfico da evolução (SVG) ---------- */
function caGraficoHTML(d){
  const s = d.serie, ant = d.anterior ? d.anterior.serie : null, ocup = d.base === 'ocupacao';
  if(!s.length) return vazio('heat', 'Sem dados no período.', '');
  const val = x => ocup ? x.presenca_s / 3600 : x.passagens;
  const vals = s.map(val), vAnt = ant ? ant.map(val) : [];
  const max = Math.max(1e-9, ...vals, ...vAnt), W = 760, H = 210, ml = 44, mb = 24, mt = 12;
  const bw = (W - ml - 8) / s.length;
  const y = v => mt + (H - mt - mb) * (1 - v / max);
  const passo = Math.max(1, Math.ceil(s.length / 12));
  const fmtV = v => ocup ? (v < 10 ? v.toFixed(1).replace('.', ',') : Math.round(v)) : fmtNum(Math.round(v));
  let svg = `<svg viewBox="0 0 ${W} ${H}" class="ca-graf" preserveAspectRatio="none">`;
  for(let i = 0; i <= 4; i++){ const v = max * i / 4, yy = y(v);
    svg += `<line x1="${ml}" x2="${W - 4}" y1="${yy}" y2="${yy}" class="ca-gl"/><text x="${ml - 6}" y="${yy + 3}" class="ca-gt" text-anchor="end">${fmtV(v)}</text>`; }
  s.forEach((x, i) => {
    const v = vals[i], xx = ml + i * bw, h = (H - mt - mb) * v / max;
    const t = 0.45 + 0.5 * v / max;
    svg += `<rect x="${xx + bw * .14}" y="${H - mb - h}" width="${Math.max(1, bw * .72)}" height="${Math.max(0, h)}" rx="2" fill="${calorTom(t)}" opacity=".9">
      <title>${caRotBalde(x.inicio, d.granularidade)} · ${ocup ? caHoras(x.presenca_s) + ' de presença' : fmtNum(x.passagens) + ' passagens'}${x.pico ? ` · pico ${x.pico}` : ''}${x.grupo >= 3 ? ` · grupo de ${x.grupo}` : ''}</title></rect>`;
    if(i % passo === 0) svg += `<text x="${xx + bw / 2}" y="${H - 7}" class="ca-gt" text-anchor="middle">${caRotBalde(x.inicio, d.granularidade)}</text>`;
  });
  if(ant && ant.length === s.length && vAnt.some(v => v > 0)){
    svg += `<polyline class="ca-ant" points="${vAnt.map((v, i) => `${ml + i * bw + bw / 2},${y(v)}`).join(' ')}"/>`;
  }
  return svg + '</svg>';
}

/* ---------- Semana × hora ---------- */
function caSemanaHTML(d){
  const m = d.semana, max = Math.max(1e-9, ...m.flat());
  const tot = r => m[r].reduce((a, b) => a + b, 0);
  const fmt = v => d.base === 'ocupacao' ? caHoras(v) : fmtNum(v);
  return `<div class="calor-semana">
    <span></span>${Array.from({length: 24}, (_, h) => `<span class="cs-hora">${h % 3 === 0 ? pad(h) : ''}</span>`).join('')}<span class="cs-hora">total</span>
    ${m.map((linha, r) => `<span class="cs-dia">${d.dias[r]}</span>${linha.map((v, h) =>
      `<i class="cs-cel" title="${d.dias[r]} ${pad(h)}h · ${fmt(v)}" style="background:${v > 0 ? calorTom(0.22 + 0.76 * Math.sqrt(v / max)) : 'var(--ink-750)'}"></i>`).join('')}
      <span class="cs-tot">${fmt(tot(r))}</span>`).join('')}
  </div>`;
}

/* ---------- Painéis ---------- */
function caKpisHTML(d){
  const k = d.kpis, a = d.anterior ? d.anterior.kpis : null, ocup = d.base === 'ocupacao';
  const kpi = (cls, lbl, icone, val, sub) => `<div class="kpi ${cls}"><div class="kpi-top"><span class="kpi-lbl">${lbl}</span>
    <span class="kpi-ico">${ico(icone, 15)}</span></div><div class="kpi-val">${val}</div><div class="kpi-delta nt">${sub}</div></div>`;
  const lim = d.aglomeracao.limite, acima = k.maior_grupo >= lim;
  return `
    ${kpi('k-cyan', 'Passagens', 'users', fmtNum(k.passagens), a ? caDelta(caVar(k.passagens, a.passagens)) : 'registros de pessoa, um por presença')}
    ${kpi('k-amber', 'Tempo de presença', 'clock', k.presenca_s ? caHoras(k.presenca_s) : '—',
          k.presenca_s ? (a ? caDelta(caVar(k.presenca_s, a.presenca_s)) : 'soma de pessoa × tempo') : 'sem ocupação gravada no período')}
    ${kpi('k-violet', 'Ocupação média', 'gauge', k.ocupacao_media != null ? String(k.ocupacao_media).replace('.', ',') : '—',
          k.ocupacao_media != null ? 'pessoas em cena, em média' : '—')}
    ${kpi('k-green', 'Pico simultâneo', 'heat', k.pico ? fmtNum(k.pico) : '—', k.pico_em ? `em ${fmtDT(k.pico_em)}` : 'pessoas ao mesmo tempo')}
    ${kpi(acima ? 'k-red' : 'k-blue', 'Maior aglomeração', 'alert', k.maior_grupo >= 3 ? `${fmtNum(k.maior_grupo)} <small>pessoas</small>` : '—',
          k.maior_grupo >= 3 ? `${acima ? 'acima' : 'abaixo'} do limite da câmera (${lim}) · ${fmtDT(k.maior_grupo_em)}` : `nenhum grupo de 3+ · limite ${lim}`)}`;
}

function caFocosHTML(d){
  if(!d.focos.length) return `<span class="hint">Sem circulação suficiente para apontar focos.</span>`;
  const max = d.focos[0].fracao;
  return d.focos.map((f, i) => `<div class="ca-foco"><span class="ca-n">${i + 1}</span>
    <div><b>${esc(f.regiao.charAt(0).toUpperCase() + f.regiao.slice(1))}</b>
      <span class="cz-barra"><i style="width:${f.fracao / max * 100}%;background:${calorTom(0.95 - i * 0.1)}"></i></span></div>
    <span class="cz-p">${(f.fracao * 100).toFixed(1).replace('.', ',')}%</span></div>`).join('');
}

function caGruposHTML(d){
  const a = d.aglomeracao, lista = a.acima_do_limite.length ? a.acima_do_limite : a.maiores;
  if(!lista.length) return `<span class="hint">Nenhum grupo de 3 ou mais pessoas juntas no período.</span>`;
  return `${a.acima_do_limite.length ? '' : `<p class="hint" style="margin:0 0 8px">Nenhum grupo chegou ao limite da câmera (${a.limite}). Os maiores:</p>`}
    ${lista.slice(0, 6).map(g => `<div class="ca-grupo ${g.pessoas >= a.limite ? 'acima' : ''}">
      <span class="ca-gn mono">${g.pessoas}</span><div><b>${g.pessoas >= a.limite ? 'Aglomeração' : 'Grupo'} de ${g.pessoas} pessoas</b>
      <span>${fmtDT(g.em)}${g.duracao_s ? ` · ${caHoras(g.duracao_s)} com grupo nesta hora` : ''}</span></div></div>`).join('')}`;
}

function caZonasHTML(d){
  const [ZL, ZC] = d.zonas_dim, ant = d.anterior && d.anterior.zonas;
  const top = d.zonas.map((p, i) => ({p, i, n: `${'ABCD'[(i / ZC) | 0]}${i % ZC + 1}`, a: ant ? ant[i] : null}))
    .filter(z => z.p > 0.001 || (z.a || 0) > 0.001).sort((x, y) => y.p - x.p).slice(0, 8);
  if(!top.length) return `<span class="hint">Sem dados.</span>`;
  return top.map(z => { const dl = z.a == null ? null : (z.p - z.a) * 100;
    return `<div class="cz-item ca-zona"><span class="cz-nome">${z.n}</span>
      <span class="cz-barra"><i style="width:${z.p / top[0].p * 100}%;background:${calorTom(0.4 + 0.55 * z.p / top[0].p)}"></i></span>
      <span class="cz-p">${(z.p * 100).toFixed(1).replace('.', ',')}%</span>
      <span class="ca-pp ${dl == null ? '' : dl > 0.5 ? 'up' : dl < -0.5 ? 'dn' : ''}">${dl == null ? '—' : `${dl >= 0 ? '+' : ''}${dl.toFixed(1).replace('.', ',')} p.p.`}</span></div>`; }).join('');
}

function caCamerasHTML(r){
  if(!r || !r.cameras.length) return `<span class="hint">Nenhuma câmera com pessoas no período.</span>`;
  const max = Math.max(1, ...r.cameras.map(c => c.presenca_s || c.passagens));
  return `<table class="fc-tbl ca-tbl"><thead><tr><th>Câmera</th><th class="r">Passagens</th>
      <th>Presença</th><th class="r">Pico</th><th class="r">Grupo</th></tr></thead><tbody>
    ${r.cameras.map(c => { const v = c.variacao == null ? null : Math.round(c.variacao * 100);
      return `<tr data-cacam="${esc(c.codigo)}" class="${c.codigo === CALOR.cam ? 'on' : ''}">
      <td><b>${esc(c.nome)}</b><span class="mono dt-sub">${esc(c.codigo)}</span></td>
      <td class="r"><b class="mono">${fmtNum(c.passagens)}</b>
        <span class="ca-d ${v == null || v === 0 ? 'nt' : v > 0 ? 'up' : 'dn'}">${v == null ? '—' : `${v > 0 ? '▲' : v < 0 ? '▼' : '='} ${Math.abs(v)}%`}</span></td>
      <td><div class="ca-barra-cel"><span class="cz-barra"><i style="width:${(c.presenca_s || c.passagens) / max * 100}%;background:${calorTom(0.45 + 0.5 * (c.presenca_s || c.passagens) / max)}"></i></span>
        <span class="mono">${c.presenca_s ? caHoras(c.presenca_s) : '—'}</span></div></td>
      <td class="r mono">${c.pico || '—'}</td><td class="r mono">${c.maior_grupo >= 3 ? c.maior_grupo : '—'}</td></tr>`; }).join('')}
    </tbody></table>`;
}

/* ---------- Carga ---------- */
async function caCarregar(){
  const corpo = $('#caCorpo'); if(!corpo) return;
  const req = ++CA.req;
  const [ini, fim] = caIntervalo();
  corpo.classList.add('ca-carregando');
  $('#caAviso') && ($('#caAviso').innerHTML = '');
  try{
    const q = {desde: ini.toISOString(), ate: fim.toISOString()};
    let [d, r] = await Promise.all([
      API.calorAnalise({...q, camera: CALOR.cam, base: CA.base === 'auto' ? 'ocupacao' : CA.base, comparar: CA.comparar}),
      API.calorCameras(q).catch(() => null)]);
    if(req !== CA.req) return;
    // Automático: a ocupação só representa o período se cobrir boa parte
    // dele. Recém-ligada, ela tem minutos — e uma semana de passagens diz
    // mais sobre a semana do que dez minutos de presença.
    if(CA.base === 'auto' && !(d.kpis.presenca_s > 0 && d.kpis.cobertura >= 0.5)){
      d = await API.calorAnalise({...q, camera: CALOR.cam, base: 'passagens', comparar: CA.comparar});
      if(req !== CA.req) return;
    }
    CA.dados = d; CA.cams = r;
    caPreencher();
  }catch(e){
    if(req !== CA.req) return;
    corpo.innerHTML = erroBloco(e, caCarregar);
  }finally{ corpo.classList.remove('ca-carregando'); }
}

function caPreencher(){
  const d = CA.dados; if(!d) return;
  $('#caKpis').innerHTML = caKpisHTML(d);
  const k = d.kpis, ocup = d.base === 'ocupacao';
  // Transparência sobre a origem dos números.
  const aviso = [];
  if(ocup){
    aviso.push(`Mapa por <b>tempo de presença</b>: cada pessoa aquece o ponto do chão onde ficou, a cada volta da análise.`);
    if(k.cobertura < 0.9) aviso.push(`A câmera esteve sob análise em ${Math.round(k.cobertura * 100)}% do período.`);
  }else{
    aviso.push(`Mapa por <b>passagens</b>: o ponto em que cada pessoa registrada apareceu.`);
    if(CA.base === 'auto') aviso.push(d.ocupacao_desde
      ? `A ocupação contínua desta câmera é gravada desde ${fmtDT(d.ocupacao_desde)}.`
      : `A ocupação contínua desta câmera ainda não tem dados gravados.`);
  }
  if(k.sem_posicao) aviso.push(`${fmtNum(k.sem_posicao)} passagem(ns) sem posição ficaram fora do mapa.`);
  $('#caAviso').innerHTML = `${ico('info', 13)}<span>${aviso.join(' ')}</span>`;
  $('#caBaseTxt').textContent = ocup ? 'tempo de presença' : 'passagens';
  $('#caGraf').innerHTML = caGraficoHTML(d);
  $('#caGrafSub').textContent = `${ocup ? 'horas de presença' : 'passagens'} por ${d.granularidade}${d.anterior ? ' · linha: período anterior' : ''}`;
  $('#caSemana').innerHTML = caSemanaHTML(d);
  $('#caFocos').innerHTML = caFocosHTML(d);
  $('#caGrupos').innerHTML = caGruposHTML(d);
  $('#caZonas').innerHTML = caZonasHTML(d);
  $('#caCams').innerHTML = caCamerasHTML(CA.cams);
  $$('#caCams [data-cacam]').forEach(tr => tr.onclick = () => {
    CALOR.cam = tr.dataset.cacam; const s = $('#calorCam'); if(s) s.value = CALOR.cam;
    caTrocarCamera();
  });
  const vazioMapa = !(d.grade.valores && d.grade.valores.length);
  $('#caMapaVazio').hidden = !vazioMapa;
  caPintarMapa();
}

function caTrocarCamera(){
  const img = $('#caFundo');
  if(img) img.src = `${API.base}/stream/${encodeURIComponent(CALOR.cam)}/snapshot?token=${encodeURIComponent(API.access)}&t=${Date.now()}`;
  const cam = (APP.cameras || []).find(c => c.codigo === CALOR.cam);
  const t = $('#caTitulo'); if(t) t.textContent = cam ? cam.nome : CALOR.cam;
  caCarregar();
}

/* ---------- Tela ---------- */
function renderCalorAnalise(corpo){
  const cam = (APP.cameras || []).find(c => c.codigo === CALOR.cam);
  const tog = (k, txt) => `<label class="calor-tog"><input type="checkbox" data-cacamada="${k}" ${CA.camadas[k] ? 'checked' : ''}><span>${txt}</span></label>`;
  corpo.innerHTML = `
  <div class="cm-barra fc-barra ca-filtros">
    <div class="segm" id="caPeriodo">${CA_PERIODOS.map(([k, v]) => `<button data-k="${k}" class="${CA.periodo === k ? 'on' : ''}">${v}</button>`).join('')}</div>
    <span id="caDatas" ${CA.periodo === 'custom' ? '' : 'hidden'} class="ca-datas">
      <input type="date" class="inp" id="caDesde" value="${esc(CA.desde)}"> <span>até</span>
      <input type="date" class="inp" id="caAte" value="${esc(CA.ate)}"></span>
    <span class="sp" style="flex:1"></span>
    <div class="segm" id="caBase">${[['auto', 'Automático'], ['ocupacao', 'Ocupação'], ['passagens', 'Passagens']].map(([k, v]) =>
      `<button data-k="${k}" class="${CA.base === k ? 'on' : ''}">${v}</button>`).join('')}</div>
    <label class="calor-tog"><input type="checkbox" id="caComparar" ${CA.comparar ? 'checked' : ''}><span>Comparar com o período anterior</span></label>
  </div>
  <div class="calor-kpis" id="caKpis">${carregando()}</div>
  <div id="caCorpo">
    <div class="calor-grade">
      <div class="panel calor-principal">
        <div class="panel-hd"><h3 id="caTitulo">${esc(cam ? cam.nome : '')}</h3><span class="sub">mapa de calor · <span id="caBaseTxt"></span></span>
          <span class="sp"></span>${tog('focos', 'Focos')}${tog('grupos', 'Aglomerações')}${tog('zonas', 'Zonas')}</div>
        <div class="panel-bd">
          <div class="calor-palco ca-palco" id="caPalco">
            <img id="caFundo" alt="" src="${API.base}/stream/${encodeURIComponent(CALOR.cam || '')}/snapshot?token=${encodeURIComponent(API.access)}">
            <canvas id="caCanvas"></canvas>
            <div class="calor-carimbo">${esc(CALOR.cam || '')} · histórico</div>
            <div class="calor-tip" id="caTip" hidden></div>
            <div class="calor-vazio" id="caMapaVazio" hidden>${vazio('heat', 'Sem movimento registrado no período.', 'Escolha outro período ou outra câmera.')}</div>
          </div>
          <div class="calor-rodape">
            <div class="calor-escala"><span>menos</span><i style="background:${calorGradiente()}"></i><span>mais</span>
              <em>intensidade relativa ao ponto mais movimentado do período</em></div>
            <p class="fc-nota cf-nota" id="caAviso" style="margin:0"></p>
          </div>
        </div></div>
      <div class="calor-lado">
        <div class="panel"><div class="panel-hd"><h3>Áreas mais movimentadas</h3><span class="sub">focos numerados no mapa</span></div>
          <div class="panel-bd" id="caFocos">${carregando()}</div></div>
        <div class="panel"><div class="panel-hd"><h3>Aglomerações</h3><span class="sub">pessoas juntas</span></div>
          <div class="panel-bd" id="caGrupos">${carregando()}</div></div>
      </div>
    </div>
    <div class="calor-grade2">
      <div class="panel"><div class="panel-hd"><h3>Evolução no período</h3><span class="sub" id="caGrafSub"></span></div>
        <div class="panel-bd" id="caGraf">${carregando()}</div></div>
      <div class="panel"><div class="panel-hd"><h3>Zonas</h3><span class="sub">participação e variação</span></div>
        <div class="panel-bd" id="caZonas">${carregando()}</div></div>
    </div>
    <div class="calor-grade2">
      <div class="panel"><div class="panel-hd"><h3>Padrão semanal</h3><span class="sub">dia da semana × hora</span></div>
        <div class="panel-bd" id="caSemana">${carregando()}</div></div>
      <div class="panel"><div class="panel-hd"><h3>Comparativo entre câmeras</h3><span class="sub">clique para abrir</span></div>
        <div class="panel-bd ca-cams" id="caCams">${carregando()}</div></div>
    </div>
  </div>`;

  $$('#caPeriodo button').forEach(b => b.onclick = () => {
    CA.periodo = b.dataset.k;
    $$('#caPeriodo button').forEach(x => x.classList.toggle('on', x === b));
    $('#caDatas').hidden = CA.periodo !== 'custom';
    if(CA.periodo === 'custom' && !CA.desde){
      CA.desde = diaISO(new Date(Date.now() - 7 * 86400e3)); CA.ate = diaISO(new Date());
      $('#caDesde').value = CA.desde; $('#caAte').value = CA.ate;
    }
    caCarregar();
  });
  $('#caDesde').onchange = e => { CA.desde = e.target.value; caCarregar(); };
  $('#caAte').onchange = e => { CA.ate = e.target.value; caCarregar(); };
  $$('#caBase button').forEach(b => b.onclick = () => {
    CA.base = b.dataset.k; $$('#caBase button').forEach(x => x.classList.toggle('on', x === b)); caCarregar(); });
  $('#caComparar').onchange = e => { CA.comparar = e.target.checked; caCarregar(); };
  $$('[data-cacamada]').forEach(i => i.onchange = () => { CA.camadas[i.dataset.cacamada] = i.checked; caPintarMapa(); });
  const palco = $('#caPalco');
  palco.onmousemove = caDica; palco.onmouseleave = () => { $('#caTip').hidden = true; };
  $('#caFundo').onerror = function(){ this.style.visibility = 'hidden'; };
  if(window.ResizeObserver){
    CA.obs && CA.obs.disconnect();
    CA.obs = new ResizeObserver(() => caPintarMapa()); CA.obs.observe(palco);
  }
  caCarregar();
}
