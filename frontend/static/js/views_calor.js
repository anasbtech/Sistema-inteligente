/* ===================== Mapa de calor — visão térmica ao vivo =====================
   A câmera ao vivo pintada como imagem térmica: a cena fica fria (azul e
   verde, pela luminosidade) e quem está em cena fica quente (amarelo, laranja
   e vermelho), com a cabeça mais quente que o corpo.

   As câmeras do parque são de vídeo comum: NÃO medem temperatura. O "calor"
   aqui é presença. O corpo quente sai das caixas que a análise publica ao vivo
   (`quadro_ao_vivo` e, para veículos, `veiculos_ao_vivo`), desenhadas com o
   formato de um corpo e desfocadas; a textura vem da própria imagem. Por isso
   a etiqueta sobre cada pessoa é o tempo em cena, e nunca graus.

   Rastro (opcional): cada pessoa ou veículo aquece o chão onde pisa, e o chão
   esfria pela metade a cada meia-vida escolhida. Ao abrir, parte do movimento
   da última hora. Os setores mais aquecidos saem desse rastro. */

const CALOR = {cam: null, caixas: true, rastro: true, meiaVida: 300, paleta: 'arco',
               C: 160, L: 90, grade: null, ultimoDecaimento: 0, ultimoEvt: {},
               emCenaPor: {}, emCena: 0, picoSimultaneo: 0, inicio: 0,
               vistos: new Map(), permanencias: [], laco: 0, relogio: null, ultimoQuadro: 0,
               rastroCampo: null, peq: null, masc: null, saida: null,
               det: {lista: [], em: 0, varr: [], varrEm: 0, n: 0, ocupado: false, semRota: false}, suave: [], detTimer: null};

const CALOR_ZONAS = {colunas: 6, linhas: 4};
const CALOR_VEICULOS = new Set(['carro', 'motocicleta', 'caminhao', 'onibus', 'bicicleta', 'picape', 'van',
  'kombi', 'caminhao_pequeno', 'caminhao_grande', 'veiculo']);
const CALOR_LARGURA = 440;            // resolução do cálculo térmico (é ampliado com suavização)
const CALOR_FPS = 12;

/* Paletas térmicas, pré-calculadas em 256 tons.
   Arco-íris: azul → ciano → verde → amarelo → vermelho (a da referência).
   Ferro: preto → violeta → vermelho → laranja → amarelo → branco, o padrão
   dos visores térmicos profissionais. */
const CALOR_PALETAS = {
  arco: [[0.00, [4, 6, 52]], [0.12, [14, 30, 158]], [0.25, [0, 96, 240]], [0.36, [0, 186, 246]],
         [0.47, [24, 226, 172]], [0.56, [64, 232, 72]], [0.65, [182, 242, 32]], [0.74, [255, 226, 0]],
         [0.84, [255, 142, 0]], [0.93, [246, 62, 12]], [1.00, [206, 14, 14]]],
  ferro: [[0.00, [2, 2, 12]], [0.14, [28, 4, 74]], [0.28, [84, 6, 128]], [0.42, [154, 18, 132]],
          [0.56, [212, 52, 78]], [0.68, [240, 104, 26]], [0.80, [251, 166, 14]], [0.91, [255, 222, 92]],
          [1.00, [255, 255, 236]]],
};
const CALOR_LUTS = {};
function calorLut(){
  const nome = CALOR_PALETAS[CALOR.paleta] ? CALOR.paleta : 'arco';
  if(CALOR_LUTS[nome]) return CALOR_LUTS[nome];
  const paradas = CALOR_PALETAS[nome], lut = new Uint8ClampedArray(256 * 3);
  for(let i = 0; i < 256; i++){
    const t = i / 255;
    let k = 1;
    while(k < paradas.length - 1 && paradas[k][0] < t) k++;
    const [t0, c0] = paradas[k - 1], [t1, c1] = paradas[k];
    const f = (t - t0) / (t1 - t0);
    for(let j = 0; j < 3; j++) lut[i * 3 + j] = c0[j] + (c1[j] - c0[j]) * f;
  }
  return (CALOR_LUTS[nome] = lut);
}
const calorTom = t => { const lut = calorLut(), i = Math.max(0, Math.min(255, Math.round(t * 255))) * 3;
  return `rgb(${lut[i]},${lut[i + 1]},${lut[i + 2]})`; };
const calorGradiente = () => `linear-gradient(90deg,${[0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1].map(calorTom).join(',')})`;

const calorTipo = c => c.tipoTerm ? c.tipoTerm : c.classe === 'pessoa' ? 'pessoa' : CALOR_VEICULOS.has(c.classe) ? 'veiculo' : (!c.classe ? 'rosto' : null);
const calorDur = s => s < 60 ? `${Math.max(0, Math.round(s))} s` : s < 3600 ? `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, '0')} s`
                                                     : `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')} min`;

/* ---------- Ao vivo: o que chega da análise ---------- */
function calorEsfriar(agora){
  const g = CALOR.grade;
  if(!g) return;
  const dt = (agora - (CALOR.ultimoDecaimento || agora)) / 1000;
  CALOR.ultimoDecaimento = agora;
  if(dt <= 0) return;
  const f = Math.pow(0.5, dt / CALOR.meiaVida);
  for(let i = 0; i < g.length; i++) g[i] *= f;
}

/* Recebe as caixas publicadas ao vivo (o laço geral e, à parte, o módulo
   veicular). Soma presença no rastro e marca há quanto tempo cada um está
   em cena, pelo `track_id`. */
function calorAoVivo(dados, doModuloVeicular){
  if(!CALOR.grade || !dados || dados.camera_codigo !== CALOR.cam) return;
  const agora = Date.now(), chave = doModuloVeicular ? 'veic' : 'geral';
  const dt = Math.min(3, Math.max(0.05, (agora - (CALOR.ultimoEvt[chave] || agora - 500)) / 1000));
  CALOR.ultimoEvt[chave] = agora;
  const {C, L, grade: g} = CALOR;
  let n = 0;
  for(const c of dados.caixas || []){
    const tipo = doModuloVeicular ? 'veiculo' : calorTipo(c);
    if(!c.bbox || (tipo !== 'pessoa' && tipo !== 'veiculo')) continue;
    const [x, y, w, h] = c.bbox;
    const px = x + w / 2, py = y + h * 0.97;
    if(!(CALOR.det.em && agora - CALOR.det.em < CALOR_DET_VALIDADE) && px >= 0 && py >= 0 && px < 1 && py < 1)
      g[((py * L) | 0) * C + ((px * C) | 0)] += dt;
    n++;
    // Com a detecção própria ativa, a permanência vem das trilhas dela: somar
    // as duas contaria a mesma pessoa duas vezes no painel.
    if(c.track_id != null && !(CALOR.det.em && agora - CALOR.det.em < CALOR_DET_VALIDADE)){
      const k = `${chave}:${c.track_id}`, v = CALOR.vistos.get(k);
      if(v){ v.ultimo = agora; v.classe = c.classe || v.classe; }
      else CALOR.vistos.set(k, {desde: agora, ultimo: agora, tipo, classe: c.classe || 'veiculo', id: c.track_id});
    }
  }
  CALOR.emCenaPor[chave] = n;
  if(CALOR.det.em && agora - CALOR.det.em < CALOR_DET_VALIDADE) return;   // a detecção própria conta melhor
  CALOR.emCena = Object.values(CALOR.emCenaPor).reduce((a, v) => a + v, 0);
  CALOR.picoSimultaneo = Math.max(CALOR.picoSimultaneo, CALOR.emCena);
}

/* Quem sumiu há mais de 8 s saiu de cena: a permanência vai para a média. */
function calorPodarVistos(agora){
  for(const [k, v] of CALOR.vistos){
    if(agora - v.ultimo > 8000){
      CALOR.vistos.delete(k);
      const s = (v.ultimo - v.desde) / 1000;
      if(s >= 2){ CALOR.permanencias.push(s); if(CALOR.permanencias.length > 300) CALOR.permanencias.shift(); }
    }
  }
}

/* ---------- Detecção própria da visão térmica ----------
   As caixas da análise geral chegam espaçadas e com o limiar de registro:
   quem está sentado, meio encoberto ou ao fundo sumia da imagem. Aqui a tela
   pede ao servidor o quadro atual analisado na hora (limiar mais baixo) e,
   a cada quarta chamada, também com a varredura em ladrilhos — que acha
   quem é pequeno demais para a passada inteira. Quem só a varredura viu
   continua na tela por alguns segundos, até a próxima varredura. */
const CALOR_DET_MS = 1100, CALOR_DET_VALIDADE = 4000, CALOR_VARR_VALIDADE = 9000;

function calorIoU(a, b){
  const x1 = Math.max(a[0], b[0]), y1 = Math.max(a[1], b[1]);
  const x2 = Math.min(a[0] + a[2], b[0] + b[2]), y2 = Math.min(a[1] + a[3], b[1] + b[3]);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  const menor = Math.min(a[2] * a[3], b[2] * b[3]) || 1;
  return inter / menor;                    // contenção: a mesma pessoa vista nas duas passadas
}

async function calorDetectar(){
  const d = CALOR.det;
  if(d.ocupado || d.semRota || APP.view !== 'heatmap' || !CALOR.cam || document.hidden) return;
  d.ocupado = true;
  const cam = CALOR.cam, varr = d.n++ % 4 === 0;
  try{
    const r = await API.calorPessoas(cam, varr);
    if(cam !== CALOR.cam) return;
    const agora = Date.now();
    const caixas = (r.caixas || []).map(c => ({...c, tipoTerm: c.classe === 'pessoa' ? 'pessoa' : 'veiculo'}));
    if(varr){ d.varr = caixas; d.varrEm = agora; }
    d.lista = caixas.filter(c => !c.varredura); d.em = agora;
    calorRastrear(caixas, varr, agora);
    // Rastro: cada corpo aquece o chão onde pisa, como o evento ao vivo faz.
    const dt = Math.min(3, (agora - (d.rastroEm || agora - CALOR_DET_MS)) / 1000);
    d.rastroEm = agora;
    const {C, L, grade: g} = CALOR;
    if(g) for(const c of calorCaixasDet()){
      const [x, y, w, h] = c.bbox, px = x + w / 2, py = y + h * 0.97;
      if(px >= 0 && py >= 0 && px < 1 && py < 1) g[((py * L) | 0) * C + ((px * C) | 0)] += dt;
    }
    const n = calorCaixasDet().length;
    CALOR.emCena = n; CALOR.picoSimultaneo = Math.max(CALOR.picoSimultaneo, n);
  }catch(e){
    // Servidor ainda sem a rota (precisa de reinício): volta às caixas gerais.
    if(e && (e.status === 404 || /404|not found/i.test(e.message || ''))) d.semRota = true;
  }finally{ d.ocupado = false; }
}

/* Rastreio da visão térmica. Cada chamada ao detector é uma foto: quem ele
   não viu numa delas (virou de lado, ficou atrás do monitor) sumia da tela e
   voltava no segundo seguinte, e um reflexo visto uma única vez virava
   "pessoa" por um segundo. Aqui cada corpo é uma trilha:
   * nasce provisória e só aparece depois de vista duas vezes — ou na hora,
     se o detector tiver confiança alta;
   * continua na tela por algumas chamadas sem ser vista (mais tempo se só a
     varredura, que roda a cada quatro chamadas, a encontra);
   * casa com a detecção nova pela sobreposição, e a caixa anda suavizada.
   O número "em cena" é o das trilhas confirmadas: não conta duas vezes quem
   ficou parado, nem quem o detector perdeu por um instante. */
const CALOR_TRILHA = {confAlta: 0.55, toques: 2, folga: 3, folgaVarredura: 6};

function calorRastrear(caixas, varr, agora){
  const d = CALOR.det;
  d.trilhas = d.trilhas || []; d.proxId = d.proxId || 1;
  const livres = new Set(d.trilhas.map((_, i) => i));
  const novas = [];
  // Mais confiantes primeiro: disputam a trilha antes das duvidosas.
  for(const c of caixas.slice().sort((a, b) => (b.confianca || 0) - (a.confianca || 0))){
    let melhor = -1, nota = 0;
    for(const i of livres){
      const t = d.trilhas[i];
      if(t.classe !== c.classe && (t.classe === 'pessoa' || c.classe === 'pessoa')) continue;
      const s = calorIoU(t.bbox, c.bbox);
      if(s > nota){ nota = s; melhor = i; }
    }
    if(melhor >= 0 && nota >= 0.3){
      livres.delete(melhor);
      const t = d.trilhas[melhor];
      t.bbox = t.bbox.map((v, k) => v + (c.bbox[k] - v) * 0.6);
      t.toques++; t.falhas = 0; t.conf = Math.max(t.conf * 0.7, c.confianca || 0); t.visto = agora;
      t.soVarredura = t.soVarredura && !!c.varredura;
    }else if(!c.varredura || varr){
      novas.push({id: d.proxId++, classe: c.classe, tipoTerm: c.tipoTerm, bbox: c.bbox.slice(), conf: c.confianca || 0,
                  toques: 1, falhas: 0, desde: agora, visto: agora, soVarredura: !!c.varredura});
    }
  }
  // Quem não foi visto: a varredura só roda a cada quatro chamadas, então a
  // trilha que só ela encontra espera por ela.
  for(const i of livres){
    const t = d.trilhas[i];
    if(!varr && t.soVarredura) continue;
    t.falhas++;
  }
  d.trilhas = d.trilhas.filter(t => t.falhas <= (t.soVarredura ? CALOR_TRILHA.folgaVarredura : CALOR_TRILHA.folga)
                                    && (t.toques > 1 || t.conf >= CALOR_TRILHA.confAlta || t.falhas === 0)).concat(novas);
  // Permanência no painel: pela trilha da própria tela.
  for(const t of calorTrilhasConfirmadas()){
    const k = `term:${t.id}`, v = CALOR.vistos.get(k);
    if(v) v.ultimo = agora;
    else CALOR.vistos.set(k, {desde: t.desde, ultimo: agora, tipo: t.classe === 'pessoa' ? 'pessoa' : 'veiculo',
                               classe: t.classe, id: t.id});
  }
}

function calorTrilhasConfirmadas(){
  const d = CALOR.det;
  return (d.trilhas || []).filter(t => t.toques >= CALOR_TRILHA.toques || t.conf >= CALOR_TRILHA.confAlta);
}

/* As caixas da detecção própria: as trilhas confirmadas. */
function calorCaixasDet(){
  const d = CALOR.det, agora = Date.now();
  if(!d.em || agora - d.em > CALOR_DET_VALIDADE) return [];
  return calorTrilhasConfirmadas().map(t => ({classe: t.classe, tipoTerm: t.tipoTerm, bbox: t.bbox,
                                              confianca: t.conf, track_id: t.id}));
}

/* O que a imagem térmica pinta agora: a detecção própria quando está fresca,
   senão as caixas publicadas pela análise geral. */
function calorCaixasAtuais(){
  const det = calorCaixasDet();
  return det.length || (CALOR.det.em && Date.now() - CALOR.det.em < CALOR_DET_VALIDADE)
    ? det : FEED.caixasDe(CALOR.cam);
}

/* Caixas suavizadas entre uma detecção e outra: cada corpo desliza até a
   posição nova em vez de saltar, e quem some esmaece em vez de piscar. */
function calorSuavizar(alvo){
  const antes = CALOR.suave || [], novas = [];
  const usado = new Set();
  for(const c of alvo){
    if(!c.bbox) continue;
    let melhor = -1, dist = 1e9;
    antes.forEach((a, i) => {
      if(usado.has(i) || (a.classe || '') !== (c.classe || '')) return;
      const dx = (a.bbox[0] + a.bbox[2] / 2) - (c.bbox[0] + c.bbox[2] / 2);
      const dy = (a.bbox[1] + a.bbox[3] / 2) - (c.bbox[1] + c.bbox[3] / 2);
      const dd = Math.hypot(dx, dy), lim = Math.max(c.bbox[2], c.bbox[3]) * 0.8;
      if(dd < lim && dd < dist){ dist = dd; melhor = i; }
    });
    if(melhor >= 0){
      usado.add(melhor);
      const a = antes[melhor];
      novas.push({...c, bbox: a.bbox.map((v, i) => v + (c.bbox[i] - v) * 0.35), vida: Math.min(1, (a.vida || 0) + 0.25)});
    }else novas.push({...c, vida: 0.3});
  }
  // Quem não veio nesta volta esmaece por alguns quadros.
  antes.forEach((a, i) => { if(!usado.has(i) && (a.vida || 0) > 0.12) novas.push({...a, vida: a.vida - 0.12}); });
  CALOR.suave = novas;
  return novas;
}

/* ---------- Desenho térmico ---------- */
function calorCanvas(nome, w, h){
  let c = CALOR[nome];
  if(!c){ c = CALOR[nome] = document.createElement('canvas'); }
  if(c.width !== w || c.height !== h){ c.width = w; c.height = h; }
  return c;
}

/* O rastro suavizado (gaussiana separável) e normalizado de 0 a 1. */
function calorRastroCampo(){
  const {C, L, grade: g} = CALOR, sigma = 2.4, r = Math.ceil(sigma * 3), k = new Float32Array(2 * r + 1);
  let s = 0;
  for(let i = -r; i <= r; i++){ k[i + r] = Math.exp(-(i * i) / (2 * sigma * sigma)); s += k[i + r]; }
  for(let i = 0; i < k.length; i++) k[i] /= s;
  const tmp = new Float32Array(C * L), out = new Float32Array(C * L);
  for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
    let v = 0; for(let i = -r; i <= r; i++){ const xx = x + i; if(xx >= 0 && xx < C) v += g[y * C + xx] * k[i + r]; }
    tmp[y * C + x] = v;
  }
  let max = 0;
  for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
    let v = 0; for(let i = -r; i <= r; i++){ const yy = y + i; if(yy >= 0 && yy < L) v += tmp[yy * C + x] * k[i + r]; }
    out[y * C + x] = v; if(v > max) max = v;
  }
  const topo = Math.max(max * 0.85, 0.6);   // piso: cena vazia não vira pico amarelo
  for(let i = 0; i < out.length; i++) out[i] = Math.min(1, out[i] / topo);
  return out;
}

/* Calor de cada corpo, em duas partes.

   1. Um molde suave dentro da caixa, desfocado: diz ONDE o corpo deve estar,
      com a cabeça mais quente.
   2. A silhueta de verdade, tirada da própria imagem: dentro da caixa, conta
      como corpo o pixel cuja cor se afasta da cor do entorno (um anel logo
      fora da caixa, que é o fundo). É o que faz o calor seguir o contorno da
      pessoa em vez de um boneco recortado.

   Veículo: bloco com a parte de baixo (motor, rodas) mais quente. Rosto
   detectado: o ponto mais quente. */
function calorMolde(m, SW, SH, caixas){
  m.globalCompositeOperation = 'source-over';
  m.fillStyle = '#000'; m.fillRect(0, 0, SW, SH);
  m.globalCompositeOperation = 'lighter';
  m.filter = `blur(${Math.max(2, SW / 95)}px)`;
  m.fillStyle = '#fff';
  const elipse = (cx, cy, rx, ry, a) => { m.globalAlpha = a; m.beginPath();
    m.ellipse(cx, cy, Math.max(1, rx), Math.max(1, ry), 0, 0, Math.PI * 2); m.fill(); };
  const bloco = (x, y, w, h, r, a) => { m.globalAlpha = a; m.beginPath();
    if(m.roundRect) m.roundRect(x, y, w, h, r); else m.rect(x, y, w, h); m.fill(); };
  for(const c of caixas){
    const tipo = calorTipo(c);
    if(!c.bbox || !tipo) continue;
    const [bx, by, bw, bh] = c.bbox, X = bx * SW, Y = by * SH, W = bw * SW, H = bh * SH;
    const vida = c.vida == null ? 1 : Math.max(0, Math.min(1, c.vida));
    if(tipo === 'pessoa'){
      const cab = Math.min(W * .24, H * .11);
      bloco(X + W * .10, Y + H * .06, W * .80, H * .90, Math.min(W, H) * .4, .34 * vida);   // contorno geral
      elipse(X + W * .50, Y + cab * 1.05, cab, cab * 1.15, .62 * vida);                    // cabeça: a mais quente
      bloco(X + W * .16, Y + H * .17, W * .68, H * .40, W * .22, .40 * vida);               // tronco e braços
      elipse(X + W * .50, Y + H * .70, W * .26, H * .26, .16 * vida);                       // quadril e pernas
    }else if(tipo === 'veiculo'){
      bloco(X + W * .04, Y + H * .10, W * .92, H * .84, Math.min(W, H) * .18, .46 * vida);
      elipse(X + W * .50, Y + H * .80, W * .42, H * .13, .30 * vida);
    }else{
      elipse(X + W * .5, Y + H * .5, W * .58, H * .62, .45 * vida);
    }
  }
  m.globalAlpha = 1; m.filter = 'none'; m.globalCompositeOperation = 'source-over';
}

function calorSilhueta(px, SW, SH, caixas){
  const fg = new Float32Array(SW * SH);
  if(!px) return fg;
  for(const c of caixas){
    const tipo = calorTipo(c);
    if(!c.bbox || (tipo !== 'pessoa' && tipo !== 'veiculo')) continue;
    const [bx, by, bw, bh] = c.bbox;
    const x0 = Math.max(0, Math.floor(bx * SW)), x1 = Math.min(SW - 1, Math.ceil((bx + bw) * SW));
    const y0 = Math.max(0, Math.floor(by * SH)), y1 = Math.min(SH - 1, Math.ceil((by + bh) * SH));
    if(x1 - x0 < 3 || y1 - y0 < 3) continue;
    const pad = Math.max(2, Math.round(Math.min(x1 - x0, y1 - y0) * .14));
    const a0 = Math.max(0, x0 - pad), a1 = Math.min(SW - 1, x1 + pad), b0 = Math.max(0, y0 - pad), b1 = Math.min(SH - 1, y1 + pad);
    const fora = (x, y) => !(x >= x0 && x <= x1 && y >= y0 && y <= y1);
    // Cor média e dispersão do entorno (o anel fora da caixa).
    let n = 0, sr = 0, sg = 0, sb = 0;
    for(let y = b0; y <= b1; y += 2) for(let x = a0; x <= a1; x += 2){
      if(!fora(x, y)) continue;
      const p = (y * SW + x) * 4; sr += px[p]; sg += px[p + 1]; sb += px[p + 2]; n++;
    }
    if(!n) continue;
    const mr = sr / n, mg = sg / n, mb = sb / n;
    let qq = 0;
    for(let y = b0; y <= b1; y += 2) for(let x = a0; x <= a1; x += 2){
      if(!fora(x, y)) continue;
      const p = (y * SW + x) * 4, dr = px[p] - mr, dg = px[p + 1] - mg, db = px[p + 2] - mb;
      qq += dr * dr + dg * dg + db * db;
    }
    const lim = 14 + 0.9 * Math.sqrt(qq / n);
    for(let y = y0; y <= y1; y++) for(let x = x0; x <= x1; x++){
      const i = y * SW + x, p = i * 4, dr = px[p] - mr, dg = px[p + 1] - mg, db = px[p + 2] - mb;
      const f = Math.min(1, Math.max(0, (Math.sqrt(dr * dr + dg * dg + db * db) - lim * .55) / (lim * 1.2)));
      if(f > fg[i]) fg[i] = f;
    }
  }
  // Uma passada de média 3×3: tira o chuvisco sem borrar o contorno.
  const out = new Float32Array(SW * SH);
  for(let y = 1; y < SH - 1; y++) for(let x = 1; x < SW - 1; x++){
    const i = y * SW + x;
    out[i] = (fg[i - SW - 1] + fg[i - SW] + fg[i - SW + 1] + fg[i - 1] + fg[i] + fg[i + 1]
            + fg[i + SW - 1] + fg[i + SW] + fg[i + SW + 1]) / 9;
  }
  return out;
}

function calorDesenhar(){
  const palco = $('#calorPalco'), cv = $('#calorCanvas'), ov = $('#calorCamadas'), video = $('#calorVideo');
  if(!palco || !cv) return;
  const W = palco.clientWidth, H = palco.clientHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
  for(const c of [cv, ov]) if(c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)){
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); c.style.width = W + 'px'; c.style.height = H + 'px'; }
  const ctx = cv.getContext('2d'), o = ov.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); o.setTransform(dpr, 0, 0, dpr, 0, 0);
  o.clearRect(0, 0, W, H);
  const tile = video && FEED.tiles.find(t => t.canvas === video), img = tile && tile.img;
  const aguardando = $('#calorAguarda');
  if(!img || !img.naturalWidth){
    ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, W, H);
    if(aguardando) aguardando.hidden = false;
    return;
  }
  if(aguardando) aguardando.hidden = true;

  const esc = Math.min(W / img.naturalWidth, H / img.naturalHeight);
  const qw = img.naturalWidth * esc, qh = img.naturalHeight * esc, qx = (W - qw) / 2, qy = (H - qh) / 2;
  const SW = CALOR_LARGURA, SH = Math.max(1, Math.round(SW * img.naturalHeight / img.naturalWidth));

  const peq = calorCanvas('peq', SW, SH), pc = peq.getContext('2d', {willReadFrequently: true});
  pc.filter = 'none'; pc.drawImage(img, 0, 0, SW, SH);
  let px, pxB;
  try{ px = pc.getImageData(0, 0, SW, SH).data; }catch(e){ px = null; }
  // A mesma imagem bem desfocada: a diferença entre as duas é o detalhe
  // local — contornos, quinas, objetos —, que vira a textura da cena.
  const borr = calorCanvas('borr', SW, SH), bc = borr.getContext('2d', {willReadFrequently: true});
  bc.filter = `blur(${Math.max(3, SW / 70)}px)`; bc.drawImage(peq, 0, 0); bc.filter = 'none';
  try{ pxB = bc.getImageData(0, 0, SW, SH).data; }catch(e){ pxB = null; }

  const caixas = calorSuavizar(calorCaixasAtuais());
  const masc = calorCanvas('masc', SW, SH), mc = masc.getContext('2d', {willReadFrequently: true});
  calorMolde(mc, SW, SH, caixas);
  const molde = mc.getImageData(0, 0, SW, SH).data;
  const sil = calorSilhueta(px, SW, SH, caixas);
  const rastro = CALOR.rastro ? CALOR.rastroCampo : null;

  const N = SW * SH, lum = new Float32Array(N), lumB = new Float32Array(N), pele = new Uint8Array(N);
  if(px) for(let i = 0, j = 0; i < N; i++, j += 4){
    const r = px[j], g = px[j + 1], b = px[j + 2];
    lum[i] = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    lumB[i] = pxB ? (0.299 * pxB[j] + 0.587 * pxB[j + 1] + 0.114 * pxB[j + 2]) / 255 : lum[i];
    // Pele pelo Cr/Cb: é o que fica mais quente numa câmera térmica de verdade
    // (rosto e mãos), mais que a roupa.
    const cr = 128 + 0.5 * r - 0.4187 * g - 0.0813 * b, cb = 128 - 0.1687 * r - 0.3313 * g + 0.5 * b;
    pele[i] = cr > 136 && cr < 178 && cb > 82 && cb < 130 && r > 60 && r > b ? 1 : 0;
  }
  const am = []; for(let i = 0; i < N; i += 23) am.push(lumB[i]);
  am.sort((a, b) => a - b);
  const lo = am[Math.floor(am.length * .02)] || 0, hi = Math.max(lo + 0.05, am[Math.floor(am.length * .98)] || 1);

  /* O corpo vira um campo contínuo: núcleo (molde × silhueta, quase sem
     desfoque) e halo (o mesmo bem desfocado). É o halo que dá a passagem
     suave fundo → verde → amarelo → laranja que todo visor térmico mostra
     na borda de uma pessoa, em vez de uma mancha recortada. */
  const corpoCv = calorCanvas('corpo', SW, SH), cc = corpoCv.getContext('2d', {willReadFrequently: true});
  const ci = cc.createImageData(SW, SH), cd = ci.data;
  for(let i = 0; i < N; i++){
    const pr = Math.min(1, molde[i * 4] / 255 * 1.25);
    let b = pr > 0.01 ? pr * (0.25 + 0.75 * sil[i]) : 0;
    const p = i * 4;
    cd[p] = Math.min(255, b * 255);                                       // R: corpo
    cd[p + 1] = pele[i] && sil[i] > 0.3 ? Math.min(255, b * 255) : 0;     // G: pele à vista
    cd[p + 2] = 0; cd[p + 3] = 255;
  }
  // O rosto é o ponto mais quente da pessoa: um disco suave onde a cabeça
  // está, limitado ao próprio corpo (não aquece o fundo atrás da cabeça).
  const rostos = calorRostos(caixas, pele, SW, SH);
  for(const [, [fx, fy, fl]] of rostos){
    const cx = fx * SW, cy = fy * SH, rr = Math.max(1.5, fl * SW * 0.42);
    for(let y = Math.max(0, (cy - rr) | 0); y <= Math.min(SH - 1, Math.ceil(cy + rr)); y++)
      for(let x = Math.max(0, (cx - rr) | 0); x <= Math.min(SW - 1, Math.ceil(cx + rr)); x++){
        const q = 1 - Math.hypot((x - cx) / rr, (y - cy) / (rr * 1.15));
        if(q <= 0) continue;
        const p = (y * SW + x) * 4, v = Math.min(255, cd[p] * Math.min(1, q * 1.8));
        if(v > cd[p + 1]) cd[p + 1] = v;
      }
  }
  cc.putImageData(ci, 0, 0);
  const nucleoCv = calorCanvas('nucleo', SW, SH), nc = nucleoCv.getContext('2d', {willReadFrequently: true});
  nc.filter = `blur(${Math.max(1, SW / 260)}px)`; nc.drawImage(corpoCv, 0, 0); nc.filter = 'none';
  const nucleo = nc.getImageData(0, 0, SW, SH).data;
  const haloCv = calorCanvas('halo', SW, SH), hc = haloCv.getContext('2d', {willReadFrequently: true});
  hc.fillStyle = '#000'; hc.fillRect(0, 0, SW, SH);
  hc.filter = `blur(${Math.max(4, SW / 48)}px)`; hc.drawImage(corpoCv, 0, 0); hc.filter = 'none';
  const halo = hc.getImageData(0, 0, SW, SH).data;
  const passo = (a, b, v) => { const t = Math.max(0, Math.min(1, (v - a) / (b - a))); return t * t * (3 - 2 * t); };

  const lut = calorLut(), saida = calorCanvas('saida', SW, SH), sc = saida.getContext('2d');
  const im = sc.createImageData(SW, SH), d = im.data, {C, L} = CALOR;
  let tMax = 0, xMax = 0, yMax = 0;
  for(let y = 0; y < SH; y++){
    const gy = Math.min(L - 1, (y / SH * L) | 0);
    for(let x = 0; x < SW; x++){
      const i = y * SW + x, p = i * 4;
      // Cena: fria e uniforme, como num visor de verdade. Só a forma geral
      // (luz do ambiente, móveis) aparece, numa faixa estreita de azuis; o
      // detalhe fino entra pouco, para os objetos se lerem sem virar ruído.
      const ln = Math.max(0, Math.min(1, (lumB[i] - lo) / (hi - lo)));
      const fino = Math.max(-0.025, Math.min(0.05, (lum[i] - lumB[i]) * 0.55));
      let t = 0.05 + 0.25 * ln + fino;
      if(rastro){ const r = rastro[gy * C + Math.min(C - 1, (x / SW * C) | 0)]; t += r * (0.46 - t) * 0.55; }
      t = Math.max(0.02, Math.min(0.40, t));
      const nu = nucleo[p] / 255, ha = halo[p] / 255, pe = nucleo[p + 1] / 255;
      if(nu > 0.004 || ha > 0.004){
        // Núcleo: roupa quente (laranja), pele mais quente (vermelho), com
        // uma leve textura das dobras vinda da imagem.
        const quente = Math.min(1, 0.70 + 0.15 * passo(0.05, 0.75, nu) + 0.17 * passo(0.08, 0.6, pe)
                                    + Math.max(-0.03, Math.min(0.03, (lum[i] - lumB[i]) * 0.35)));
        const peso = Math.max(passo(0.02, 0.42, nu), 0.62 * passo(0.0, 0.5, ha));
        t += (quente - t) * peso;
      }
      if(t > tMax){ tMax = t; xMax = x; yMax = y; }
      const k = Math.max(0, Math.min(255, (t * 255) | 0)) * 3;
      d[p] = lut[k]; d[p + 1] = lut[k + 1]; d[p + 2] = lut[k + 2]; d[p + 3] = 255;
    }
  }
  sc.putImageData(im, 0, 0);
  ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, W, H);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.filter = 'blur(0.8px)';
  ctx.drawImage(saida, qx, qy, qw, qh);
  ctx.filter = 'none';
  // Vinheta da lente: os cantos um pouco mais escuros, como no visor.
  const vin = ctx.createRadialGradient(qx + qw / 2, qy + qh / 2, Math.min(qw, qh) * 0.42, qx + qw / 2, qy + qh / 2, Math.hypot(qw, qh) * 0.58);
  vin.addColorStop(0, 'rgba(2,4,18,0)'); vin.addColorStop(1, 'rgba(2,4,18,.42)');
  ctx.fillStyle = vin; ctx.fillRect(qx, qy, qw, qh);

  if(CALOR.caixas) calorDesenharMarcas(o, caixas, qx, qy, qw, qh, rostos);
  calorHud(o, qx, qy, qw, qh, tMax > 0.62 ? [qx + (xMax + .5) / SW * qw, qy + (yMax + .5) / SH * qh] : null);
}

/* Onde está o rosto de cada pessoa: os pixels de pele no alto da caixa.
   Sem pele à vista (de costas, longe demais), a posição provável da cabeça. */
function calorRostos(caixas, pele, SW, SH){
  const out = new Map();
  for(const c of caixas){
    if(!c.bbox || calorTipo(c) !== 'pessoa' || (c.vida != null && c.vida <= 0.35)) continue;
    const [bx, by, bw, bh] = c.bbox;
    // Só o alto e o centro da caixa: mesa de madeira e braço também passam por pele.
    const x0 = Math.max(0, Math.floor((bx + bw * .2) * SW)), x1 = Math.min(SW - 1, Math.ceil((bx + bw * .8) * SW));
    const y0 = Math.max(0, Math.floor(by * SH)), y1 = Math.min(SH - 1, Math.ceil((by + bh * 0.22) * SH));
    let n = 0, mx = 1e9, Mx = -1, my = 1e9, My = -1;
    for(let y = y0; y <= y1; y++) for(let x = x0; x <= x1; x++) if(pele[y * SW + x]){
      n++; if(x < mx) mx = x; if(x > Mx) Mx = x; if(y < my) my = y; if(y > My) My = y; }
    const lado = Math.max(bw * 0.34, Math.min(bw * 0.55, bh * 0.16));
    let r;
    if(n >= 5 && (Mx - mx) / SW < bw * 0.6 && (My - my) / SH < bh * 0.2){
      const cx = (mx + Mx + 1) / 2 / SW, cy = (my + My + 1) / 2 / SH;
      const l = Math.max(lado, Math.max(Mx - mx + 1, My - my + 1) / SW * 1.25);
      r = [cx, cy, l];                          // centro e lado (fração da largura)
    }else{
      r = [bx + bw / 2, by + lado * 0.5 * SW / SH, lado];
    }
    out.set(c, r);
  }
  return out;
}

/* Instrumento sobre a imagem: escala vertical, data e hora, ponto mais
   quente. Como num visor térmico, mas sem graus: aqui a escala é presença. */
function calorHud(o, qx, qy, qw, qh, pico){
  const lut = calorLut(), bw = 10, bh = Math.min(220, qh * .5), bx = qx + qw - bw - 16, by = qy + (qh - bh) / 2;
  const g = o.createLinearGradient(0, by + bh, 0, by);
  for(let i = 0; i <= 10; i++){ const k = Math.round(i / 10 * 255) * 3; g.addColorStop(i / 10, `rgb(${lut[k]},${lut[k + 1]},${lut[k + 2]})`); }
  o.fillStyle = 'rgba(5,7,13,.55)'; o.fillRect(bx - 8, by - 22, bw + 16, bh + 44);
  o.fillStyle = g; o.fillRect(bx, by, bw, bh);
  o.strokeStyle = 'rgba(255,255,255,.55)'; o.lineWidth = 1; o.strokeRect(bx + .5, by + .5, bw - 1, bh - 1);
  o.fillStyle = 'rgba(255,255,255,.85)'; o.font = '600 9px "IBM Plex Mono", monospace'; o.textAlign = 'center';
  o.textBaseline = 'bottom'; o.fillText('ALTA', bx + bw / 2, by - 6);
  o.textBaseline = 'top'; o.fillText('BAIXA', bx + bw / 2, by + bh + 6);
  for(let i = 1; i < 4; i++){ const y = by + bh * i / 4; o.beginPath(); o.moveTo(bx - 4, y + .5); o.lineTo(bx, y + .5); o.stroke(); }

  const agora = new Date();
  const txt = `${agora.toLocaleDateString('pt-BR')}  ${agora.toLocaleTimeString('pt-BR')}`;
  o.textAlign = 'left'; o.textBaseline = 'middle'; o.font = '600 11px "IBM Plex Mono", monospace';
  const tw = o.measureText(txt).width;
  // Canto de baixo à direita: o da esquerda costuma ter o letreiro da câmera.
  o.fillStyle = 'rgba(5,7,13,.6)'; o.fillRect(qx + qw - tw - 28, qy + qh - 32, tw + 16, 22);
  o.fillStyle = '#fff'; o.fillText(txt, qx + qw - tw - 20, qy + qh - 21);

  if(pico){
    const [x, y] = pico;
    o.strokeStyle = 'rgba(255,255,255,.95)'; o.lineWidth = 1.3;
    o.beginPath();
    for(const [a, b, c2, e] of [[x - 11, y, x - 4, y], [x + 4, y, x + 11, y], [x, y - 11, x, y - 4], [x, y + 4, x, y + 11]]){ o.moveTo(a, b); o.lineTo(c2, e); }
    o.stroke();
    o.font = '600 9.5px "IBM Plex Mono", monospace';
    o.fillStyle = '#fff'; o.fillText('MÁX', x + 14, y);
  }
  o.textAlign = 'start';
}

/* Marcações no estilo de um visor térmico: um quadrado branco fino no rosto
   de cada pessoa e o retângulo do veículo, com uma etiqueta curta (P1, P2…
   ou o tipo do veículo). Sem graus: estas câmeras não medem temperatura. O
   tempo de permanência fica no painel ao lado. */
function calorDesenharMarcas(o, caixas, qx, qy, qw, qh, rostos){
  const ordem = caixas.filter(c => c.bbox && calorTipo(c) && calorTipo(c) !== 'rosto' && (c.vida == null || c.vida > 0.35))
    .sort((a, b) => (a.bbox[0] - b.bbox[0]) || (a.bbox[1] - b.bbox[1]));
  o.font = '600 9.5px "IBM Plex Mono", monospace'; o.textBaseline = 'bottom'; o.textAlign = 'left';
  let np = 0;
  for(const c of ordem){
    const tipo = calorTipo(c);
    let x, y, w, h, nome;
    if(tipo === 'pessoa'){
      const r = rostos && rostos.get(c);
      if(!r) continue;
      w = h = Math.max(9, r[2] * qw);
      x = qx + r[0] * qw - w / 2; y = qy + r[1] * qh - h / 2;
      nome = `P${++np}`;
    }else{
      const [bx, by, bw, bh] = c.bbox;
      x = qx + bx * qw; y = qy + by * qh; w = bw * qw; h = bh * qh;
      nome = String((typeof ROTULO_OBJ !== 'undefined' && ROTULO_OBJ[c.classe]) || 'veículo').toUpperCase();
    }
    o.globalAlpha = c.vida == null ? 1 : Math.min(1, c.vida + .2);
    x = Math.round(x) + .5; y = Math.round(y) + .5; w = Math.round(w); h = Math.round(h);
    // Cantoneiras, não o retângulo inteiro: marca sem tampar o calor.
    const l = Math.max(4, Math.min(w, h) * 0.28);
    o.shadowColor = 'rgba(0,0,0,.7)'; o.shadowBlur = 3;
    o.lineWidth = 1.6; o.strokeStyle = 'rgba(255,255,255,.96)';
    o.beginPath();
    for(const [cx, cy, sx, sy] of [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]){
      o.moveTo(cx + sx * l, cy); o.lineTo(cx, cy); o.lineTo(cx, cy + sy * l);
    }
    o.stroke();
    o.shadowBlur = 0;
    // Etiqueta em plaqueta escura, acima da marca.
    const tw = o.measureText(nome).width + 8, ty = Math.max(qy + 1, y - 15);
    o.fillStyle = 'rgba(4,8,20,.72)'; o.fillRect(x - .5, ty, tw, 13);
    o.fillStyle = '#fff'; o.fillText(nome, x + 3.5, ty + 11);
  }
  o.globalAlpha = 1; o.textAlign = 'start'; o.textBaseline = 'alphabetic';
}

/* ---------- Painéis ---------- */
function calorFatias(){
  const {C, L, grade: g} = CALOR, {colunas: ZC, linhas: ZL} = CALOR_ZONAS, soma = new Array(ZC * ZL).fill(0);
  let tot = 0;
  if(g) for(let y = 0; y < L; y++) for(let x = 0; x < C; x++){
    const n = g[y * C + x]; if(!n) continue;
    soma[Math.min(ZL - 1, (y * ZL / L) | 0) * ZC + Math.min(ZC - 1, (x * ZC / C) | 0)] += n; tot += n;
  }
  return soma.map((n, i) => ({nome: 'ABCD'[(i / ZC) | 0] + ((i % ZC) + 1), n, p: tot ? n / tot : 0, i}));
}

function calorZonasHTML(){
  const top = calorFatias().filter(z => z.n > 0.5).sort((a, b) => b.n - a.n).slice(0, 5);
  if(!top.length) return `<span class="hint">Ainda sem movimento recente.</span>`;
  return top.map((z, i) => `<div class="cz-item"><span class="cz-nome">${z.nome}</span>
    <span class="cz-barra"><i style="width:${(z.p / top[0].p) * 100}%;background:${calorTom(0.95 - i * 0.1)}"></i></span>
    <span class="cz-p">${(z.p * 100).toFixed(1).replace('.', ',')}%</span></div>`).join('');
}

function calorEmCenaHTML(){
  const agora = Date.now();
  const lista = [...CALOR.vistos.values()].filter(v => agora - v.ultimo < 4000).sort((a, b) => a.desde - b.desde).slice(0, 10);
  if(!lista.length) return `<span class="hint">Ninguém em cena agora.</span>`;
  const nome = v => v.tipo === 'pessoa' ? 'Pessoa' : (typeof ROTULO_OBJ !== 'undefined' && ROTULO_OBJ[v.classe]) || 'Veículo';
  return lista.map(v => { const s = (agora - v.desde) / 1000;
    return `<div class="term-item"><span class="term-ponto" style="background:${calorTom(Math.min(1, 0.7 + s / 600))}"></span>
      <span class="term-nome">${esc(nome(v))} <small>#${esc(String(v.id))}</small></span>
      <span class="term-t">${calorDur(s)}${v.desde - CALOR.inicio < 3000 ? '+' : ''}</span></div>`; }).join('');
}

function calorKpisHTML(){
  const min = Math.floor((Date.now() - CALOR.inicio) / 60000);
  const p = CALOR.permanencias, media = p.length ? p.reduce((a, b) => a + b, 0) / p.length : null;
  const kpi = (cls, lbl, icone, val, sub) => `<div class="kpi ${cls}"><div class="kpi-top"><span class="kpi-lbl">${lbl}</span>
    <span class="kpi-ico">${ico(icone, 15)}</span></div><div class="kpi-val">${val}</div><div class="kpi-delta nt">${sub}</div></div>`;
  return `
    ${kpi('k-red', 'Em cena agora', 'heat', fmtNum(CALOR.emCena), 'pessoas e veículos')}
    ${kpi('k-amber', 'Pico simultâneo', 'alert', fmtNum(CALOR.picoSimultaneo), 'desde que a tela abriu')}
    ${kpi('k-violet', 'Permanência média', 'clock', media != null ? calorDur(media) : '—', p.length ? `de ${fmtNum(p.length)} que já saíram` : 'ninguém saiu ainda')}
    ${kpi('k-green', 'Observando há', 'gauge', min < 1 ? '<1 min' : `${fmtNum(min)} min`, 'o rastro parte da última hora')}`;
}

function calorAtualizarPaineis(){
  const k = $('#calorKpis'); if(k) k.innerHTML = calorKpisHTML();
  const e = $('#calorEmCena'); if(e) e.innerHTML = calorEmCenaHTML();
  const z = $('#calorZonasLista'); if(z) z.innerHTML = calorZonasHTML();
  const i = $('#calorVivoInfo');
  if(i) i.innerHTML = `<span class="cv-pulso"></span><b>AO VIVO</b><span class="cv-item"><em>${fmtNum(CALOR.emCena)}</em> em cena</span>`;
}

/* ---------- Ciclo ---------- */
async function calorIniciar(){
  const ativo = () => APP.view === 'heatmap' && $('#calorPalco');
  CALOR.grade = new Float32Array(CALOR.C * CALOR.L);
  CALOR.ultimoDecaimento = Date.now(); CALOR.ultimoEvt = {}; CALOR.emCenaPor = {}; CALOR.emCena = 0;
  CALOR.picoSimultaneo = 0; CALOR.inicio = Date.now(); CALOR.vistos = new Map(); CALOR.permanencias = [];
  CALOR.det = {lista: [], em: 0, varr: [], varrEm: 0, n: 0, ocupado: false, semRota: CALOR.det && CALOR.det.semRota}; CALOR.suave = [];
  clearInterval(CALOR.detTimer);
  CALOR.detTimer = setInterval(() => { if(!ativo()){ clearInterval(CALOR.detTimer); return; } calorDetectar(); }, CALOR_DET_MS);
  // Rastro inicial: o movimento da última hora, já esfriado pela idade média.
  const desde = new Date(Date.now() - 3600e3).toISOString(), f = Math.pow(0.5, 1800 / CALOR.meiaVida);
  for(const camada of ['pessoas', 'veiculos']){
    try{
      const r = await API.calorMapa({camera: CALOR.cam, camada, desde});
      if(r.colunas === CALOR.C && r.linhas === CALOR.L)
        for(let y = 0; y < CALOR.L; y++) for(let x = 0; x < CALOR.C; x++) CALOR.grade[y * CALOR.C + x] += r.grade[y][x] * f;
    }catch(e){ /* sem histórico recente: o rastro começa do zero */ }
  }
  cancelAnimationFrame(CALOR.laco);
  const quadro = ts => {
    if(!ativo()) return;
    CALOR.laco = requestAnimationFrame(quadro);
    if(document.hidden || ts - CALOR.ultimoQuadro < 1000 / CALOR_FPS) return;
    CALOR.ultimoQuadro = ts;
    calorDesenhar();
  };
  CALOR.laco = requestAnimationFrame(quadro);
  clearInterval(CALOR.relogio);
  CALOR.relogio = setInterval(() => {
    if(!ativo()){ clearInterval(CALOR.relogio); return; }
    const agora = Date.now();
    calorEsfriar(agora); calorPodarVistos(agora); calorAtualizarPaineis();
    CALOR.rastroCampo = calorRastroCampo();
  }, 1000);
  CALOR.rastroCampo = calorRastroCampo();
  calorAtualizarPaineis();
  calorDetectar();
}

async function renderMapaCalor(alvo){
  cancelAnimationFrame(CALOR.laco); clearInterval(CALOR.relogio); clearInterval(CALOR.detTimer);
  const cams = APP.cameras && APP.cameras.length ? APP.cameras : await API.cameras();
  if(!CALOR.cam || !cams.some(c => c.codigo === CALOR.cam))
    CALOR.cam = (cams.find(c => c.status === 'ONLINE') || cams[0] || {}).codigo || null;
  const cam = cams.find(c => c.codigo === CALOR.cam);
  const tog = (id, on, txt) => `<label class="calor-tog"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span>${txt}</span></label>`;
  const aba = CALOR.aba === 'analise' ? 'analise' : 'vivo';
  const cabecalho = `
  <div class="sec-hd"><h2>Mapa de calor</h2><span class="sub">${aba === 'vivo' ? 'visão térmica ao vivo' : 'análise histórica de circulação'}</span><span class="sp"></span>
    <div class="toolbar">
      <div class="segm" id="calorAba">${[['vivo', 'Ao vivo'], ['analise', 'Análise']].map(([k, v]) =>
        `<button data-k="${k}" class="${aba === k ? 'on' : ''}">${v}</button>`).join('')}</div>
      <select class="inp" id="calorCam">${cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === CALOR.cam ? 'selected' : ''}>
        ${esc(c.codigo)} · ${esc(c.nome)}</option>`).join('')}</select>
    </div></div>`;
  const ligarCabecalho = () => {
    $$('#calorAba button').forEach(b => b.onclick = () => { if(CALOR.aba !== b.dataset.k){ CALOR.aba = b.dataset.k; renderMapaCalor(alvo); } });
  };
  if(aba === 'analise'){
    // Análise: sem vídeo e sem detecção ao vivo — só o que foi gravado.
    alvo.innerHTML = cabecalho + '<div id="calorAnalise"></div>';
    ligarCabecalho();
    $('#calorCam').onchange = e => { CALOR.cam = e.target.value; caTrocarCamera(); };
    renderCalorAnalise($('#calorAnalise'));
    return;
  }
  alvo.innerHTML = `${cabecalho}
  <div class="calor-kpis term-kpis" id="calorKpis"></div>
  <div class="calor-grade" id="calorCorpo">
    <div class="panel calor-principal">
      <div class="panel-hd"><h3>${esc(cam ? cam.nome : '')}</h3><span class="sub">o calor é presença: quem está em cena aquece</span><span class="sp"></span>
        <div class="segm" id="calorPaleta">${[['arco', 'Arco-íris'], ['ferro', 'Ferro']].map(([k, v]) =>
          `<button data-k="${k}" class="${CALOR.paleta === k ? 'on' : ''}">${v}</button>`).join('')}</div>
        ${tog('calorCaixasOn', CALOR.caixas, 'Marcações')}
        ${tog('calorRastroOn', CALOR.rastro, 'Rastro')}
        <label class="calor-ctl">Rastro esfria pela metade a cada
          <select class="inp" id="calorMeia">${[[60, '1 min'], [300, '5 min'], [900, '15 min']].map(([v, t]) =>
            `<option value="${v}" ${CALOR.meiaVida === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      </div>
      <div class="panel-bd">
        <div class="calor-palco term-palco" id="calorPalco">
          ${cam ? `<canvas id="calorVideo" class="term-fonte"></canvas>` : ''}
          <canvas id="calorCanvas"></canvas>
          <canvas id="calorCamadas"></canvas>
          <div class="calor-carimbo">${esc(cam ? cam.codigo : '')} · térmico</div>
          <div class="calor-vivo-info" id="calorVivoInfo"></div>
          <div class="calor-vazio" id="calorAguarda" hidden>${vazio('heat', cam ? 'Aguardando a imagem da câmera…' : 'Nenhuma câmera cadastrada.', '')}</div>
        </div>
        <div class="calor-rodape">
          <div class="calor-escala"><span>frio</span><i style="background:${calorGradiente()}"></i><span>quente</span>
            <em>temperatura aparente pela presença — estas câmeras não medem temperatura</em></div>
        </div>
      </div></div>
    <div class="calor-lado">
      <div class="panel"><div class="panel-hd"><h3>Em cena agora</h3><span class="sub">tempo de permanência</span></div>
        <div class="panel-bd" id="calorEmCena">${carregando()}</div></div>
      <div class="panel"><div class="panel-hd"><h3>Setores mais aquecidos</h3><span class="sub">pelo rastro</span></div>
        <div class="panel-bd" id="calorZonasLista">${carregando()}</div></div>
    </div>
  </div>
  <p class="hint calor-nota">Visual térmico simulado sobre o vídeo comum: a cena fica fria pela luminosidade e cada pessoa ou
    veículo detectado aquece. Nenhuma temperatura é medida e nenhuma pessoa é identificada nesta tela.</p>`;

  ligarCabecalho();
  $('#calorCam').onchange = e => { CALOR.cam = e.target.value; renderMapaCalor(alvo); };
  $$('#calorPaleta button').forEach(b => b.onclick = () => {
    CALOR.paleta = b.dataset.k;
    $$('#calorPaleta button').forEach(x => x.classList.toggle('on', x === b));
    const faixa = $('.calor-escala i'); if(faixa) faixa.style.background = calorGradiente();
    calorAtualizarPaineis();
  });
  $('#calorCaixasOn').onchange = e => { CALOR.caixas = e.target.checked; };
  $('#calorRastroOn').onchange = e => { CALOR.rastro = e.target.checked; };
  $('#calorMeia').onchange = e => { CALOR.meiaVida = +e.target.value; };
  if(cam){
    FEED.register($('#calorVideo'), cam, {zonas: false, boxes: false, hd: true, conter: true});
    FEED.start();
  }
  await calorIniciar();
}
