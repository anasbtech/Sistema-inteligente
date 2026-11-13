/* ==========================================================
   SEGURANÇA — Registros de detecção e base biométrica
   ========================================================== */
'use strict';

const PAG = {facial:1, objetos:1, acoes:1, bio:1, alertas:1, audit:1};
const FILTRO = {
  facial:  {q:'', status:'', cam:'', match:'', de:'', ate:''},
  objetos: {q:'', classe:'', cam:'', de:'', ate:''},
  acoes:   {q:'', classe:'', cam:'', de:'', ate:''},
  bio:     {q:'', sexo:'', status:''},
  alertas: {status:'', severidade:'', cam:'', de:'', ate:'', por_pagina:0},
  audit:   {q:'', acao:''}
};
const POR_PAGINA = 14;

const opcoesCameras = (filtro, sel) => APP.cameras
  .filter(filtro || (() => true))
  .map(c => `<option value="${esc(c.codigo)}" ${sel === c.codigo ? 'selected' : ''}>${esc(c.codigo)} — ${esc(c.nome.slice(0,28))}</option>`)
  .join('');

function ligarPager(raiz, chave, recarregar){
  $$('[data-pg]', raiz).forEach(b => b.onclick = () => {
    PAG[chave] = +b.dataset.pg.split(':')[1];
    recarregar();
  });
}

/* Placa lida: o recorte da placa (quando guardado) com o texto embaixo, no
   formato de placa. O recorte é o que permite conferir a leitura — no
   recorte do carro inteiro a placa tem poucos pixels. */
function placaHTML(d, tamanho){
  const img = d.placa_snapshot_key && API.access
    ? `<img class="placa-img" alt="Placa ${esc(d.placa)}" loading="lazy"
         src="${API.base}/deteccoes/${encodeURIComponent(d.codigo)}/placa?token=${encodeURIComponent(API.access)}">`
    : '';
  return `<span class="placa-lida ${tamanho || 'mini'}">${img}<b class="placa-txt">${esc(d.placa)}</b></span>`;
}

const barraConfianca = (v, cor) => `<div class="conf">
  <div class="conf-bar"><i style="width:${Math.round((v||0)*100)}%;background:${cor}"></i></div>
  <b>${pct100(v)}</b></div>`;

/* ---------- Período do calendário ----------
   O banco guarda UTC e trata como UTC o que chega sem fuso (app/tipos.py), e
   `new Date('2026-09-22')` também é lido como UTC pelo padrão da linguagem.
   Por isso a data do calendário é montada campo a campo, em hora local: o dia
   escolhido vale do primeiro ao último instante do dia de quem está olhando.
   Os dois extremos importam — parar em 00:00 deixaria o dia final inteiro de
   fora, e um intervalo de 22 a 25 mostraria só até o 24. */

const limiteDia = (valor, fim) => {
  if(!valor) return undefined;
  const [a, m, d] = valor.split('-').map(Number);
  return (fim ? new Date(a, m-1, d, 23, 59, 59, 999) : new Date(a, m-1, d)).toISOString();
};

// Sem passar por Date: o formato do calendário já traz ano, mês e dia prontos,
// e reinterpretá-los devolveria o deslocamento de fuso que limiteDia evita.
const fmtDia = v => v ? v.split('-').reverse().join('/') : '';

function rotuloPeriodo(de, ate){
  if(de && ate) return de === ate ? fmtDia(de) : `${fmtDia(de)} a ${fmtDia(ate)}`;
  if(de)  return `de ${fmtDia(de)} em diante`;
  if(ate) return `até ${fmtDia(ate)}`;
  return 'todo o período registrado';
}

/* ==========================================================
   RECONHECIMENTO FACIAL
   ========================================================== */
/* Similaridade com a marca do limiar: de relance, quanto ficou acima ou
   abaixo do ponto em que o sistema passa a afirmar a identidade. */
function fcSimilaridade(sim, limiar, identificada){
  const v = Math.max(0, Math.min(1, sim || 0)), l = Math.max(0, Math.min(1, limiar || 0));
  const cor = identificada ? 'var(--green)' : v >= l * 0.8 ? 'var(--amber)' : 'var(--txt-3)';
  return `<div class="fc-sim" title="Similaridade ${pct100(sim)} · limiar ${Math.round(l * 100)}%">
    <div class="fc-sim-trilho"><i style="width:${(v * 100).toFixed(1)}%;background:${cor}"></i>
      ${l ? `<em style="left:${(l * 100).toFixed(1)}%"></em>` : ''}</div>
    <b class="mono">${pct100(sim)}</b></div>`;
}

async function renderFacial(alvo){
  const f = FILTRO.facial, hoje = diaISO(new Date());
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Reconhecimento facial</h2><span class="cm-sub">registros de identificação biométrica</span><span class="sp"></span></div>
  <div class="dash-grid fc-kpis" id="fcKpis">${carregando()}</div>
  <div class="cm-barra fc-barra">
    <div class="search">${ico('search',14)}<input class="inp" id="fcQ" value="${esc(f.q)}" placeholder="ID biométrico (BIO-M-000004)…"></div>
    <div class="segm" id="fcMatch">${[['','Todas'],['true','Identificadas'],['false','Não identificadas']].map(([k,l]) =>
      `<button data-k="${k}" class="${f.match === k ? 'on' : ''}">${l} <em data-cont="${k}"></em></button>`).join('')}</div>
    <select class="inp" id="fcCam"><option value="">Todas as câmeras</option>${opcoesCameras(c => c.analitico_facial, f.cam)}</select>
    <div class="periodo">
      <label for="fcDe">Período</label>
      <input class="inp" id="fcDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || hoje)}">
      <span class="sep">até</span>
      <input class="inp" id="fcAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${hoje}">
      <button class="btn btn-ghost btn-sm" id="fcLimpar" ${f.de||f.ate?'':'hidden'}>Limpar</button>
    </div>
  </div>
  <p class="fc-nota" title="O identificador não muda entre capturas, e o sexo vem do cadastro da pessoa — não de inferência sobre a imagem. Captura sem correspondência espera a triagem, na Base biométrica, onde vira um BIO-N — que passa a BIO-M ou BIO-F quando a pessoa for cadastrada.">
    ${ico('info',13)}<span>Cada correspondência vincula a captura a um <b>identificador biométrico permanente</b>; captura sem correspondência aguarda a <b>triagem</b>.</span></p>
  <div class="panel"><div class="panel-bd tight"><div id="fcCorpo">${carregando()}</div></div></div>`;

  const filtros = () => ({
    tipo: 'FACIAL',
    camera: FILTRO.facial.cam || undefined,
    id_biometrico: /^BIO-[MFN]-\d{6}$/i.test(FILTRO.facial.q) ? FILTRO.facial.q.toUpperCase() : undefined,
    desde: limiteDia(FILTRO.facial.de),
    ate: limiteDia(FILTRO.facial.ate, true)
  });

  // Indicadores do recorte (câmera e período), sem o filtro de correspondência.
  const carregarKpis = async () => {
    try{
      const [com, sem] = await Promise.all([
        API.deteccoes({...filtros(), apenas_match: 'true', por_pagina: 1}),
        API.deteccoes({...filtros(), apenas_match: 'false', por_pagina: 1})]);
      const nc = com.total || 0, ns = sem.total || 0, tot = nc + ns;
      const kpi = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
        <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
        <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
      $('#fcKpis').innerHTML = `
        ${kpi('', 'Capturas faciais', 'face', fmtNum(tot), esc(rotuloPeriodo(FILTRO.facial.de, FILTRO.facial.ate)), dashBarra(1, 'var(--acc)'))}
        ${kpi('k-green', 'Identificadas', 'check', fmtNum(nc), `${tot ? fmtPct(nc / tot * 100) : '—'} das capturas`, dashBarra(tot ? nc / tot : 0, 'var(--green)'))}
        ${kpi('k-amber', 'Não identificadas', 'search', fmtNum(ns), 'aguardam triagem na Base biométrica', dashBarra(tot ? ns / tot : 0, 'var(--amber)'))}`;
      const conta = {'': tot, 'true': nc, 'false': ns};
      $$('#fcMatch [data-cont]').forEach(e => e.textContent = fmtNum(conta[e.dataset.cont]));
    }catch(e){ $('#fcKpis').innerHTML = ''; }
  };

  const carregar = async () => {
    const corpo = $('#fcCorpo');
    corpo.innerHTML = carregando();
    FILTRO.facial = {...FILTRO.facial, q: $('#fcQ').value.trim(), cam: $('#fcCam').value,
                     de: $('#fcDe').value, ate: $('#fcAte').value};
    try{
      const r = await API.deteccoes({...filtros(), pagina: PAG.facial, por_pagina: POR_PAGINA,
                                     apenas_match: FILTRO.facial.match || undefined});
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl">
        <thead><tr><th style="width:74px">Captura</th><th>Pessoa</th><th>Situação</th>
          <th style="width:220px">Similaridade</th><th>Câmera</th><th>Quando</th><th>Revisão</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => {
          const p = d.pessoa, st = p ? (STATUS_PESSOA[p.status]||{}) : null;
          return `<tr class="clickable" data-det="${esc(d.codigo)}">
            <td><div class="fc-cap ${p ? 'ok' : 'nao'}"><canvas data-snap="${snapAttr(d)}" width="46"></canvas></div></td>
            <td>${p ? `<div class="fc-pessoa">
                <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="32" height="32"></canvas>
                <div><b>${esc(p.nome)}</b><span class="mono">${esc(p.id_biometrico)} · ${esc({M:'Masculino',F:'Feminino',N:'Não informado'}[p.sexo])}</span></div></div>`
              // Sem cadastro o identificador ainda não existe: o que se mostra
              // é para onde a captura vai, não um número que não foi emitido.
              : `<div class="fc-pessoa nao"><span class="fc-avatar">${ico('face',15)}</span>
                <div><b>Não identificada</b><span class="mono">BIO-N a emitir na triagem</span></div></div>`}</td>
            <td>${st ? `<span class="chip ${st.cls}">${esc(st.lbl)}</span>` : '<span class="chip c-gray">Desconhecida</span>'}</td>
            <td>${fcSimilaridade(d.similaridade, d.limiar_aplicado, !!p)}</td>
            <td><b class="fc-cam">${esc(d.camera?d.camera.codigo:'')}</b><span class="fc-sub">${esc((d.camera?d.camera.nome:'').slice(0,26))}</span></td>
            <td><span class="fc-hora mono">${fmtDT(d.ocorrido_em)}</span><span class="fc-sub">${relTime(d.ocorrido_em)}</span></td>
            <td>${d.revisado ? `<span class="fc-rev ok">${ico('check',11)} ${esc(d.veredito||'Revisado')}</span>` : '<span class="fc-rev">Pendente</span>'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="7">${vazio('face','Nenhum registro encontrado.','Ajuste os filtros ou aguarde novas capturas.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'facial')}`;
      pintarFaces(corpo); pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'facial', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => {
    PAG.facial = 1; $('#fcDe').max = $('#fcAte').value || hoje; $('#fcAte').min = $('#fcDe').value;
    $('#fcLimpar').hidden = !($('#fcDe').value || $('#fcAte').value);
    carregar().then(carregarKpis);
  };
  $('#fcQ').oninput = debounce(aplicar, 350);
  $$('#fcMatch button').forEach(b => b.onclick = () => {
    FILTRO.facial.match = b.dataset.k;
    $$('#fcMatch button').forEach(x => x.classList.toggle('on', x === b));
    PAG.facial = 1; carregar();
  });
  ['fcCam','fcDe','fcAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#fcLimpar').onclick = () => { $('#fcDe').value = ''; $('#fcAte').value = ''; aplicar(); };
  await carregar();
  carregarKpis();
}

/* ==========================================================
   OBJETOS
   ========================================================== */
const OB_VEICULOS = new Set(['carro', 'motocicleta', 'caminhao', 'onibus', 'bicicleta', 'picape', 'van', 'kombi',
  'caminhao_pequeno', 'caminhao_grande', 'veiculo']);
const obIcone = k => k === 'pessoa' ? 'users' : OB_VEICULOS.has(k) ? 'car' : 'box';
const SEV_COR = ['', 'var(--txt-3)', 'var(--acc)', 'var(--amber)', 'var(--red)'];

async function renderObjetos(alvo){
  // `veiculo` fica fora do filtro: é o rótulo genérico dos registros anteriores
  // à separação por tipo e da cena sintética, e carro, caminhão, ônibus e
  // motocicleta já cobrem o caso. Continua em ROTULO_OBJ porque a tabela ainda
  // precisa saber traduzi-lo onde ele já foi gravado.
  // Históricas: o detector não as emite mais, mas registros antigos ainda
  // as têm — o rótulo fica em ROTULO_OBJ, a opção sai do filtro.
  const FORA_DO_FILTRO = ['veiculo', 'animal', 'passaro', 'cavalo', 'boi', 'ovelha',
                          'tesoura', 'bastao', 'volume'];
  const classes = Object.entries(ROTULO_OBJ).filter(([k]) => !FORA_DO_FILTRO.includes(k));
  /* Capacete e arma de fogo não existem no modelo do parque: dependem de um
     detector próprio, que pode não estar instalado. A opção continua na lista
     — tirá-la mandaria quem a procura vasculhar a tela — mas diz ali mesmo
     que não há detector, em vez de devolver lista vazia para sempre e deixar
     o operador achar que não houve ocorrência. */
  const semDetector = Object.entries(((APP.cfg || {}).detectores_extras) || {})
    .filter(([, pronto]) => !pronto).map(([k]) => k);
  // Nome próprio, e não `rotuloClasse`: com o mesmo nome esta função escondia
  // a global de ui.js, e a coluna Classe da tabela saía vazia em toda linha.
  const rotuloOpcao = (k, v) => semDetector.includes(k) ? `${v} — sem detector` : v;

  const o = FILTRO.objetos, hoje = diaISO(new Date());
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Detecção de objetos</h2><span class="cm-sub">classificação de objetos por câmera</span><span class="sp"></span></div>
  <div class="dash-grid dash-kpis" id="obKpis">${carregando()}</div>
  <div class="cm-barra fc-barra">
    <select class="inp" id="obClasse"><option value="">Todas as classes</option>
      ${classes.map(([k,v]) => `<option value="${k}" ${o.classe===k?'selected':''}
        ${semDetector.includes(k) ? 'disabled' : ''}>${esc(rotuloOpcao(k, v))}</option>`).join('')}</select>
    <select class="inp" id="obCam"><option value="">Todas as câmeras</option>${opcoesCameras(null, o.cam)}</select>
    <div class="periodo">
      <label for="obDe">Período</label>
      <input class="inp" id="obDe" type="date" value="${esc(o.de)}" max="${esc(o.ate || hoje)}">
      <span class="sep">até</span>
      <input class="inp" id="obAte" type="date" value="${esc(o.ate)}" min="${esc(o.de)}" max="${hoje}">
      <button class="btn btn-ghost btn-sm" id="obLimpar" ${o.de||o.ate?'':'hidden'}>Limpar</button>
    </div>
    <span class="sp"></span>
    <span class="cm-cont" id="obCont"></span>
  </div>
  <div class="panel" style="margin-bottom:14px"><div class="panel-hd"><h3>Classes detectadas</h3>
      <span class="sub" id="obGraficoSub">${esc(rotuloPeriodo(o.de, o.ate))}</span><span class="sp"></span>
      <span class="hint" style="margin:0">clique numa classe para filtrar a lista</span></div>
    <div class="panel-bd" id="obGrafico">${carregando()}</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="obCorpo">${carregando()}</div></div></div>`;

  // Uma consulta só, agrupada no banco. O filtro de câmera entra junto: o
  // gráfico tem de falar do mesmo recorte que a tabela abaixo dele, senão
  // mostra o parque inteiro enquanto a lista mostra uma câmera.
  const carregarGrafico = async () => {
    const g = $('#obGrafico');
    if(!g) return;
    g.innerHTML = carregando();
    const {de, ate, cam} = FILTRO.objetos;
    const sub = $('#obGraficoSub');
    if(sub) sub.textContent = rotuloPeriodo(de, ate);
    try{
      const r = await API.deteccoesPorClasse({tipo:'OBJETO', camera: cam || undefined,
        desde: limiteDia(de), ate: limiteDia(ate, true)});
      const todas = Object.entries(r.classes || {}).filter(([, v]) => v > 0);
      const tot = todas.reduce((a, [, v]) => a + v, 0);
      const pes = (r.classes || {}).pessoa || 0;
      const vei = todas.filter(([k]) => OB_VEICULOS.has(k)).reduce((a, [, v]) => a + v, 0);
      const kpi = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
        <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
        <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
      $('#obKpis').innerHTML = `
        ${kpi('', 'Detecções', 'box', fmtNum(tot), `${todas.length} classe(s) no período`, dashBarra(1, 'var(--acc)'))}
        ${kpi('k-green', 'Pessoas', 'users', fmtNum(pes), `${pctCurto(pes, tot)} das detecções`, dashBarra(tot ? pes / tot : 0, 'var(--green)'))}
        ${kpi('k-violet', 'Veículos', 'car', fmtNum(vei), `${pctCurto(vei, tot)} das detecções`, dashBarra(tot ? vei / tot : 0, 'var(--violet)'))}
        ${kpi('k-amber', 'Outros objetos', 'search', fmtNum(Math.max(0, tot - pes - vei)), 'mochilas, celulares e afins', dashBarra(tot ? (tot - pes - vei) / tot : 0, 'var(--amber)'))}`;

      const visiveis = classes
        .map(([k]) => ({k, nome: ROTULO_OBJ[k], v: (r.classes || {})[k] || 0, cor: COR_CLASSE[k] || 'var(--acc)'}))
        .filter(c => c.v > 0).sort((x, y) => y.v - x.v);
      if(!visiveis.length){
        g.innerHTML = vazio('box', 'Nenhuma detecção neste período.', 'Escolha outro intervalo no calendário.');
        return;
      }
      const max = visiveis[0].v, soma = visiveis.reduce((a, c) => a + c.v, 0);
      // Barra em escala de raiz: Pessoa (dezenas de milhares) não apaga a
      // classe de poucas dezenas. O número e a fatia ao lado são a medida.
      g.innerHTML = `<div class="ob-classes" style="--linhas:${Math.ceil(visiveis.length / 2)}">${visiveis.map((c, i) => `
        <button type="button" class="ob-classe ${FILTRO.objetos.classe === c.k ? 'on' : ''}" data-classe="${esc(c.k)}">
          <span class="ob-pos">${pad(i + 1)}</span>
          <span class="ob-ico" style="color:${c.cor};background:color-mix(in srgb, ${c.cor} 15%, transparent)">${ico(obIcone(c.k), 14)}</span>
          <span class="ob-nome">${esc(c.nome)}</span>
          <span class="ob-barra"><i style="width:${(Math.sqrt(c.v / max) * 100).toFixed(1)}%;background:${c.cor}"></i></span>
          <b class="mono">${fmtNum(c.v)}</b><em class="mono">${pctCurto(c.v, soma)}</em>
        </button>`).join('')}</div>`;
      $$('.ob-classe', g).forEach(b => b.onclick = () => {
        const k = b.dataset.classe;
        $('#obClasse').value = FILTRO.objetos.classe === k ? '' : k;
        aplicar();
      });
    }catch(e){ g.innerHTML = erroBloco(e); }
  };

  const carregar = async () => {
    const corpo = $('#obCorpo');
    corpo.innerHTML = carregando();
    try{
      const r = await API.deteccoes({
        tipo:'OBJETO', pagina: PAG.objetos, por_pagina: POR_PAGINA,
        classe: FILTRO.objetos.classe || undefined,
        camera: FILTRO.objetos.cam || undefined,
        desde: limiteDia(FILTRO.objetos.de),
        ate: limiteDia(FILTRO.objetos.ate, true)
      });
      const cont = $('#obCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} registro(s)`;
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl">
        <thead><tr><th style="width:74px">Captura</th><th>Classe</th><th style="width:220px">Confiança</th><th>Câmera</th>
          <th>Placa</th><th>Quando</th><th>Severidade</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => {
          const cor = COR_CLASSE[d.classe] || 'var(--acc)';
          return `<tr class="clickable" data-det="${esc(d.codigo)}">
            <td><div class="fc-cap" style="--cap:${cor}"><canvas data-snap="${snapAttr(d)}" width="46"></canvas></div></td>
            <td><div class="fc-pessoa"><span class="ob-ico" style="color:${cor};background:color-mix(in srgb, ${cor} 15%, transparent)">${ico(obIcone(d.classe), 14)}</span>
              <div><b>${esc(rotuloClasse(d))}</b><span class="mono">${esc(d.codigo)}</span></div></div></td>
            <td>${fcSimilaridade(d.confianca, 0, true).replace('var(--green)', cor)}</td>
            <td><b class="fc-cam">${esc(d.camera?d.camera.codigo:'')}</b><span class="fc-sub">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td>${d.placa ? placaHTML(d, 'mini') : '<span class="fc-sub">—</span>'}</td>
            <td><span class="fc-hora mono">${fmtDT(d.ocorrido_em)}</span><span class="fc-sub">${relTime(d.ocorrido_em)}</span></td>
            <td><span class="ob-sev" style="--sev:${SEV_COR[d.severidade] || 'var(--txt-3)'}"><i></i>${esc(SEVERIDADE[d.severidade])}</span></td>
          </tr>`;
        }).join('') || `<tr><td colspan="7">${vazio('box','Nenhuma detecção encontrada.',
            `Período: ${esc(rotuloPeriodo(FILTRO.objetos.de, FILTRO.objetos.ate))}.`)}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'objetos')}`;
      pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'objetos', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => {
    PAG.objetos = 1;
    FILTRO.objetos = {classe: $('#obClasse').value, cam: $('#obCam').value,
                      de: $('#obDe').value, ate: $('#obAte').value, q:''};
    // O próprio calendário passa a recusar um fim anterior ao início; sem isto
    // a busca voltaria vazia sem dizer que o intervalo é que está invertido.
    $('#obDe').max = FILTRO.objetos.ate || hoje;
    $('#obAte').min = FILTRO.objetos.de;
    $('#obLimpar').hidden = !(FILTRO.objetos.de || FILTRO.objetos.ate);
    carregar();
    carregarGrafico();
  };

  ['obClasse','obCam','obDe','obAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#obLimpar').onclick = () => { $('#obDe').value = ''; $('#obAte').value = ''; aplicar(); };

  await Promise.all([carregar(), carregarGrafico()]);
}

/* ==========================================================
   AÇÕES
   ========================================================== */
async function renderAcoes(alvo){
  const classes = Object.entries(ROTULO_ACAO_UI), a = FILTRO.acoes, hoje = diaISO(new Date());
  // Quantas ações têm detector de fato: vem do catálogo do servidor. Sem ele,
  // conta todas — é o que a tela dizia antes.
  const catalogo = (APP.cfg && Array.isArray(APP.cfg.acoes)) ? APP.cfg.acoes : [];
  const disponiveis = catalogo.length ? catalogo.filter(x => x.disponivel).length : classes.length;
  // EPI e sinal de socorro vêm de módulos próprios, fora do catálogo de ações:
  // a severidade é a que o servidor grava neles (vision.SEVERIDADE_ACAO).
  const FORA_DO_CATALOGO = {epi_ausente: 3, sinal_socorro: 4};
  const sevDe = k => ((catalogo.find(x => x.codigo === k) || {}).severidade) || FORA_DO_CATALOGO[k] || 2;
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Análise de ações e comportamento</h2><span class="cm-sub">eventos comportamentais detectados</span><span class="sp"></span></div>
  <div class="dash-grid dash-kpis" id="acKpis">${carregando()}</div>
  <div class="cm-barra fc-barra">
    <select class="inp" id="acClasse"><option value="">Todas as ações</option>
      ${classes.map(([k,v]) => `<option value="${k}" ${a.classe===k?'selected':''}>${esc(v)}</option>`).join('')}</select>
    <select class="inp" id="acCam"><option value="">Todas as câmeras</option>${opcoesCameras(c => c.analitico_acao, a.cam)}</select>
    <div class="periodo">
      <label for="acDe">Período</label>
      <input class="inp" id="acDe" type="date" value="${esc(a.de)}" max="${esc(a.ate || hoje)}">
      <span class="sep">até</span>
      <input class="inp" id="acAte" type="date" value="${esc(a.ate)}" min="${esc(a.de)}" max="${hoje}">
      <button class="btn btn-ghost btn-sm" id="acLimpar" ${a.de||a.ate?'':'hidden'}>Limpar</button>
    </div>
    <span class="sp"></span>
    <span class="cm-cont" id="acCont"></span>
  </div>
  <div class="dash-grid ac-linha">
    <div class="panel s12"><div class="panel-hd"><h3>Ações por tipo</h3><span class="sub" id="acGraficoSub">${esc(rotuloPeriodo(a.de, a.ate))}</span>
        <span class="sp"></span><span class="hint" style="margin:0">clique num tipo para filtrar a lista</span></div>
      <div class="panel-bd dash-rola" id="acGrafico">${carregando()}</div></div>
  </div>
  <div class="panel"><div class="panel-bd tight"><div id="acCorpo">${carregando()}</div></div></div>`;

  const carregarGrafico = async () => {
    const g = $('#acGrafico');
    if(!g) return;
    g.innerHTML = carregando();
    const {de, ate, cam} = FILTRO.acoes;
    const sub = $('#acGraficoSub');
    if(sub) sub.textContent = rotuloPeriodo(de, ate);
    try{
      const r = await API.deteccoesPorClasse({tipo:'ACAO', camera: cam || undefined,
        desde: limiteDia(de), ate: limiteDia(ate, true)});
      const visiveis = classes
        .map(([k]) => ({k, nome: ROTULO_ACAO_UI[k], v: (r.classes || {})[k] || 0, sev: sevDe(k)}))
        .filter(c => c.v > 0).sort((x, y) => y.v - x.v);
      const tot = visiveis.reduce((s, c) => s + c.v, 0);
      const crit = visiveis.filter(c => c.sev >= 4).reduce((s, c) => s + c.v, 0);
      const aten = visiveis.filter(c => c.sev === 3).reduce((s, c) => s + c.v, 0);
      const kpi = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
        <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
        <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
      $('#acKpis').innerHTML = `
        ${kpi('k-violet', 'Eventos', 'run', fmtNum(tot), `${visiveis.length} tipo(s) no período`, dashBarra(1, 'var(--violet)'))}
        ${kpi('k-red', 'Críticos', 'alert', fmtNum(crit), `${pctCurto(crit, tot)} · exigem confirmação humana`, dashBarra(tot ? crit / tot : 0, 'var(--red)'))}
        ${kpi('k-amber', 'Atenção', 'eye', fmtNum(aten), `${pctCurto(aten, tot)} dos eventos`, dashBarra(tot ? aten / tot : 0, 'var(--amber)'))}
        ${kpi('k-green', 'Comportamentos ativos', 'check', `${disponiveis}<small>/${classes.length}</small>`, 'com detector funcionando', dashBarra(classes.length ? disponiveis / classes.length : 0, 'var(--green)'))}`;
      if(!visiveis.length){ g.innerHTML = vazio('run', 'Nenhuma ação neste período.', 'Escolha outro intervalo no calendário.'); return; }
      const max = visiveis[0].v;
      g.innerHTML = `<div class="ac-lista">${visiveis.map((c, i) => `
        <button type="button" class="ac-tipo ${FILTRO.acoes.classe === c.k ? 'on' : ''}" data-classe="${esc(c.k)}" style="--sev:${SEV_COR[c.sev] || 'var(--violet)'}">
          <span class="ob-pos">${pad(i + 1)}</span>
          <span class="ac-nome">${esc(c.nome)}</span>
          <span class="ac-sev">${esc(SEVERIDADE[c.sev] || '')}</span>
          <span class="ob-barra"><i style="width:${(c.v / max * 100).toFixed(1)}%;background:var(--sev)"></i></span>
          <b class="mono">${fmtNum(c.v)}</b><em class="mono">${pctCurto(c.v, tot)}</em>
        </button>`).join('')}</div>`;
      $$('.ac-tipo', g).forEach(b => b.onclick = () => {
        const k = b.dataset.classe;
        $('#acClasse').value = FILTRO.acoes.classe === k ? '' : k;
        aplicar();
      });
    }catch(e){ g.innerHTML = erroBloco(e); }
  };

  const carregar = async () => {
    const corpo = $('#acCorpo');
    corpo.innerHTML = carregando();
    try{
      const r = await API.deteccoes({
        tipo:'ACAO', pagina: PAG.acoes, por_pagina: POR_PAGINA,
        classe: FILTRO.acoes.classe || undefined, camera: FILTRO.acoes.cam || undefined,
        desde: limiteDia(FILTRO.acoes.de),
        ate: limiteDia(FILTRO.acoes.ate, true)
      });
      const cont = $('#acCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} registro(s)`;
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl">
        <thead><tr><th style="width:74px">Captura</th><th>Ação detectada</th><th style="width:210px">Confiança</th>
          <th>Duração</th><th>Envolvidos</th><th>Câmera</th><th>Quando</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => {
          const cor = SEV_COR[d.severidade] || 'var(--violet)';
          return `<tr class="clickable" data-det="${esc(d.codigo)}">
            <td><div class="fc-cap" style="--cap:${cor}"><canvas data-snap="${snapAttr(d)}" width="46"></canvas></div></td>
            <td><div class="fc-pessoa"><span class="ob-ico" style="color:${cor};background:color-mix(in srgb, ${cor} 15%, transparent)">${ico('run', 14)}</span>
              <div><b>${esc(rotuloClasse(d))}</b><span class="mono">${esc(d.codigo)} · ${esc(SEVERIDADE[d.severidade] || '')}</span></div></div></td>
            <td>${fcSimilaridade(d.confianca, 0, true).replace('var(--green)', 'var(--violet)')}</td>
            <td><span class="fc-hora mono">${duracao(d.duracao_s)}</span></td>
            <td><span class="ac-env">${ico('users', 13)} ${d.envolvidos || 1}</span></td>
            <td><b class="fc-cam">${esc(d.camera?d.camera.codigo:'')}</b><span class="fc-sub">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td><span class="fc-hora mono">${fmtDT(d.ocorrido_em)}</span><span class="fc-sub">${relTime(d.ocorrido_em)}</span></td>
          </tr>`;
        }).join('') || `<tr><td colspan="7">${vazio('run','Nenhum evento comportamental encontrado.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'acoes')}`;
      pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'acoes', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => { PAG.acoes = 1; FILTRO.acoes = {classe: $('#acClasse').value, cam: $('#acCam').value,
                      de: $('#acDe').value, ate: $('#acAte').value, q:''}; $('#acDe').max = FILTRO.acoes.ate || hoje; $('#acAte').min = FILTRO.acoes.de; $('#acLimpar').hidden = !(FILTRO.acoes.de || FILTRO.acoes.ate); carregar(); carregarGrafico(); };
  ['acClasse','acCam','acDe','acAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#acLimpar').onclick = () => { $('#acDe').value = ''; $('#acAte').value = ''; aplicar(); };
  await Promise.all([carregar(), carregarGrafico()]);
}

/* ==========================================================
   DETALHE DA DETECÇÃO
   ========================================================== */
/* Vincula uma captura a quem já está cadastrado, usando-a como amostra.

   O template feito de foto de documento casa mal com o rosto de 80 pixels
   que a câmera do corredor entrega — ângulo, lente e luz são outros. Daí
   cadastrar pelo próprio material que se vai reconhecer. Quem diz de quem é
   o rosto é quem está olhando o recorte; o servidor só refaz o vetor. */
async function vincularCapturaAPessoa(d){
  const box = abrirModal(`<div class="modal-bd">${carregando('Carregando cadastros…')}</div>`);
  let pessoas = [];
  try{ pessoas = (await API.pessoas({por_pagina: 200})).itens || []; }
  catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  box.innerHTML = `
    <div class="modal-hd"><h3>Vincular captura a um cadastro</h3>
      <span class="chip c-blue mono">${esc(d.codigo)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="note" style="margin-bottom:13px">${ico('info',15)}<div>
        O rosto desta captura passa a ser <b>uma amostra do template</b> da pessoa
        escolhida, e as próximas passagens dela tendem a ser reconhecidas.
        Vincular a pessoa errada ensina o sistema a errar — confira o recorte antes.
      </div></div>
      <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:14px">
        <canvas id="vincSnap" width="132" height="132"
                style="border:1px solid var(--line);border-radius:var(--r-m);background:#000;flex:none"></canvas>
        <div style="min-width:0">
          <div class="eyebrow" style="margin-bottom:5px">Captura</div>
          <div class="mono" style="font-size:11.5px;color:var(--txt-3);line-height:1.7">
            ${esc(d.camera ? d.camera.nome : '—')}<br>${fmtDT(d.ocorrido_em)}<br>
            qualidade ${pct100(d.qualidade_face)}</div>
        </div>
      </div>
      <div class="search" style="margin-bottom:9px">${ico('search',14)}
        <input class="inp" id="vincQ" placeholder="Filtrar por nome ou ID biométrico…"></div>
      <div id="vincLista" class="rank" style="max-height:260px;overflow:auto"></div>
    </div>
    <div class="modal-ft"><button class="btn btn-ghost" data-close>Cancelar</button></div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  carregarRecorte($('#vincSnap'), d.codigo);

  const pintar = () => {
    const q = $('#vincQ').value.trim().toLowerCase();
    const vis = pessoas.filter(p => !q || p.nome.toLowerCase().includes(q)
                                 || p.id_biometrico.toLowerCase().includes(q));
    $('#vincLista').innerHTML = vis.map(p => {
      const st = STATUS_PESSOA[p.status] || {};
      return `<div class="rank-row clickable" data-bio="${esc(p.id_biometrico)}" style="cursor:pointer">
        <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="34" height="34"></canvas>
        <div class="rank-bd"><b>${esc(p.nome)}</b>
          <span class="sub">${esc(p.id_biometrico)} · ${p.amostras || 0} amostra(s)</span></div>
        <span class="chip ${st.cls}">${esc(st.lbl)}</span>
      </div>`;
    }).join('') || vazio('face','Nenhum cadastro encontrado.');
    pintarFaces($('#vincLista'));
    $$('#vincLista .rank-row').forEach(r => r.onclick = () => vincular(r.dataset.bio));
  };

  const vincular = async bio => {
    const p = pessoas.find(x => x.id_biometrico === bio);
    const ok = await confirmar('Vincular captura',
      `Esta captura passa a ser amostra do template de <b>${esc(p.nome)}</b> ` +
      `(${esc(bio)}).<br><br>O vínculo fica na trilha de auditoria com a sua matrícula.`,
      {rotulo:'Vincular'});
    // `confirmar` toma o lugar deste modal; desistindo, a lista volta.
    if(!ok) return vincularCapturaAPessoa(d);
    try{
      const r = await API.amostrasDeCapturas(bio, [d.codigo]);
      fecharModal();
      const semAmostra = (r.recusadas || []).some(x => x.vinculada);
      toast('Captura vinculada',
        semAmostra
          ? `${esc(p.nome)} — vinculada à ficha. O rosto não pôde ser extraído desta imagem, então ela não virou amostra do reconhecimento.`
          : `${esc(p.nome)} — ${r.amostras} amostra(s) no template.`, 'ok');
      irPara(APP.view);
    }catch(e){ erroToast(e); }
  };

  $('#vincQ').oninput = debounce(pintar, 200);
  pintar();
}

async function abrirDeteccao(codigo){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let d;
  try{ d = await API.deteccao(codigo); }
  catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const p = d.pessoa;
  const podeRevisar = API.pode('edit') && API.pode('facial') && !d.revisado;
  // Descartar é para o registro que nasceu errado no detector; falso positivo
  // é revisão e continua no banco, alimentando a reavaliação do limiar. Por
  // isso o descarte fica atrás de `settings`, como o expurgo, e não de `edit`.
  const podeDescartar = API.pode('settings') && !d.pessoa;
  const naoIdentificada = d.tipo === 'FACIAL' && !p;

  box.className = 'modal wide pf-modal dt-modal';
  const corSexo = p ? ({M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo]) : 'var(--acc)';
  const facial = d.tipo === 'FACIAL';
  const rotTipo = facial ? (p ? 'Identificação positiva' : 'Captura não identificada')
                : d.tipo === 'OBJETO' ? 'Detecção de objeto' : 'Análise de ações';
  const titulo = facial ? (p ? p.nome : 'Rosto sem cadastro correspondente') : rotuloClasse(d);
  const revisaoChip = d.revisado
    ? `<span class="chip ${d.veredito === 'Falso positivo' ? 'c-red' : 'c-green'}">${esc(d.veredito || 'Revisado')}</span>`
    : '<span class="chip c-amber">Revisão pendente</span>';
  const medida = facial ? d.similaridade : d.confianca;
  const ok = facial ? !!p : (medida || 0) >= (d.limiar_aplicado || 0);
  const linha = (rot, val, mono) => `<div class="pf-kv"><span>${rot}</span><b class="${mono ? 'mono' : ''}">${val}</b></div>`;
  box.innerHTML = `
    <div class="modal-hd pf-hd">
      <div class="pf-hd-t"><em style="${facial && !p ? 'color:var(--amber)' : ''}">${esc(rotTipo)}</em>
        <h3>${esc(titulo)}</h3>
        <div class="cs-hd-meta">
          <span class="mono">${esc(d.codigo)}</span>
          ${d.camera ? `<span>${ico('cam', 12)}${esc(d.camera.nome)}</span>` : ''}
          <span class="mono">${fmtDT(d.ocorrido_em)}</span>
          <span>${revisaoChip}</span>
        </div></div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="dt-grade">
        <div class="dt-foto">
          <div id="detMold" class="dt-mold"><canvas id="detBig" style="display:block"></canvas></div>
          <span class="dt-foto-pe">${ico('snap', 12)} Recorte da captura · ${d.camera ? esc(d.camera.codigo) : ''}</span>
        </div>
        <div class="dt-info">
          ${p ? `<div class="dt-pessoa" style="--sx:${corSexo}">
            <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="58" height="58"></canvas>
            <div style="min-width:0;flex:1">
              <span class="dt-pessoa-t">Registro biométrico vinculado</span>
              <b>${esc(p.nome)}</b>
              <span class="mono" style="color:${corSexo}">${esc(p.id_biometrico)}</span>
            </div>
            <span class="chip ${(STATUS_PESSOA[p.status]||{}).cls}">${esc((STATUS_PESSOA[p.status]||{}).lbl)}</span>
          </div>` : ''}
          <div class="dt-medida ${ok ? 'ok' : 'baixo'}">
            <div class="dt-medida-hd"><span>${facial ? 'Similaridade com o cadastro' : 'Confiança do detector'}</span>
              <b class="mono">${pct100(medida)}</b></div>
            ${fcSimilaridade(medida, d.limiar_aplicado || 0, ok)}
            <span class="dt-medida-pe">limiar da câmera ${Math.round((d.limiar_aplicado || 0) * 100)}% · ${ok
              ? (facial ? 'acima do limiar: identificação atribuída' : 'acima do limiar')
              : (facial ? 'abaixo do limiar: não atribuída a ninguém' : 'abaixo do limiar')}</span>
          </div>
          <div class="pf-card dt-dados">
            ${linha('Câmera', d.camera ? `${esc(d.camera.nome)} <span class="dt-sub mono">${esc(d.camera.codigo)}</span>` : '—')}
            ${linha('Data / hora', fmtDT(d.ocorrido_em), true)}
            ${facial ? `
              ${linha('Qualidade da face', pct100(d.qualidade_face), true)}
              ${linha('Sexo (cadastro)', p ? esc(ROTULO_SEXO[p.sexo]) : '—')}`
            : d.tipo === 'OBJETO' ? `
              ${linha('Classe', esc(rotuloClasse(d)))}
              ${d.placa ? linha('Placa (OCR)', placaHTML(d, 'grande')) : ''}`
            : `
              ${linha('Duração', duracao(d.duracao_s), true)}
              ${linha('Envolvidos', `${d.envolvidos || 1} pessoa(s)`, true)}`}
            ${linha('Severidade', `<span class="chip ${CLS_SEV[d.severidade]}">${esc(SEVERIDADE[d.severidade])}</span>`)}
          </div>
          <p class="dt-tec" title="${esc(d.hash_evidencia || '')}">${facial ? 'Embedding 512-D · distância cosseno · ' : ''}evidência <span class="mono">${esc(d.snapshot_key || '—')}</span>${d.hash_evidencia ? ` · hash <span class="mono">${esc(String(d.hash_evidencia).slice(0, 16))}…</span>` : ''}</p>
        </div>
      </div>
      ${naoIdentificada ? `<p class="fc-nota cf-nota">${ico('alert',13)}<span>Similaridade abaixo do limiar da câmera: a captura <b>não</b> foi atribuída a ninguém. O recorte é descartado no prazo de retenção, salvo vinculação a inquérito.</span></p>` : ''}
    </div>
    <div class="modal-ft">
      ${podeDescartar ? `<button class="btn btn-ghost btn-sm" id="detDel">${ico('trash',14)} Descartar</button>` : ''}
      ${p && podeRevisar && facial ? `<button class="btn btn-danger btn-sm" id="detFalso">${ico('x',14)} Não é esta pessoa</button>` : ''}
      <span class="sp" style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-close>Fechar</button>
      ${p && API.pode('biometrics') ? `<button class="btn btn-soft btn-sm" id="detPessoa">${ico('db',14)} Abrir ficha</button>` : ''}
      ${naoIdentificada && API.pode('biometrics') && API.pode('edit')
        ? `<button class="btn btn-soft btn-sm" id="detVincular">${ico('face',14)} Vincular a cadastro</button>
           <button class="btn btn-soft btn-sm" id="detCadastrar">${ico('db',14)} Cadastrar na base</button>` : ''}
      ${d.camera && d.camera.codigo !== 'VIDEO-TESTE' ? `<button class="btn btn-soft btn-sm" id="detCam">${ico('live',14)} Ver câmera</button>` : ''}
      ${podeRevisar ? `<button class="btn btn-primary btn-sm" id="detConf">${ico('check',14)} Confirmar</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  requestAnimationFrame(() => {
    const big = $('#detBig');
    if(big){
      // Medido na moldura, não no canvas: o canvas agora encolhe até o
      // recorte, e medir nele daria a largura da imagem anterior.
      const w = Math.round($('#detMold').getBoundingClientRect().width);
      molduraVazia(big, w, Math.round(w * .6));
      carregarRecorte(big, d.codigo, w);
    }
  });

  const bp = $('#detPessoa');
  if(bp) bp.onclick = () => { fecharModal(); abrirPessoa(d.pessoa.id_biometrico); };
  const bc = $('#detCam');
  if(bc) bc.onclick = () => { fecharModal(); abrirCamera(d.camera.codigo); };
  const bcad = $('#detCadastrar');
  if(bcad) bcad.onclick = () => { fecharModal(); editarPessoa(null, {deteccao: d.codigo}); };
  const bvinc = $('#detVincular');
  if(bvinc) bvinc.onclick = () => vincularCapturaAPessoa(d);

  /* A saída que vale para qualquer registro travado — com alerta ou com
     pessoa. Preserva o registro, tira da conta de acertos e alimenta a
     reavaliação do limiar, que é o que de fato corrige o detector. */
  const marcarFalsoPositivo = async () => {
    try{
      await API.revisar(d.codigo, 'Falso positivo');
      fecharModal();
      toast('Marcado como falso positivo',
            `${d.codigo} — registrado na trilha de auditoria.`, 'warn');
      irPara(APP.view);
    }catch(e){ erroToast(e); }
  };

  const bdel = $('#detDel');
  if(bdel) bdel.onclick = async () => {
    /* O que impede a exclusão é dito *antes*, não depois.

       Antes, quem tentasse apagar um registro preso a alerta escrevia o
       motivo, confirmava, e só então recebia a recusa — com duas saídas
       sugeridas no texto e nenhuma delas ao alcance, numa outra tela. A regra
       continua: apagar por baixo de um alerta o deixaria apontando para o
       vazio. O que muda é que o impedimento aparece quando ainda dá para
       escolher outro caminho. */
    const alertas = d.alertas || [];
    const presoPessoa = !!d.pessoa;

    /* Identificou alguém: não há caminho para apagar, e isso é a regra — o
       registro pode integrar inquérito. O que se oferece é a revisão. */
    if(presoPessoa){
      const ok = await confirmar('Este registro não pode ser apagado',
        `Ele <b>identificou ${esc(d.pessoa.nome)}</b> e pode integrar inquérito.
         <br><br>Se a identificação está errada, marque como <b>falso
         positivo</b>: o registro permanece, deixa de contar como acerto e
         alimenta a reavaliação do limiar — que é o que corrige o detector.`,
        {rotulo: podeRevisar ? 'Marcar falso positivo' : 'Entendi'});
      if(ok && podeRevisar) await marcarFalsoPositivo();
      return;
    }

    /* Preso a alerta: aqui existe caminho, e ele precisa ser o verdadeiro.
       A mensagem antiga mandava "encerrar o alerta antes" — medido, encerrar
       põe o alerta em RESOLVIDO e **mantém** o vínculo, então a exclusão
       continuava recusada. O operador fazia o que o sistema pedia e batia na
       mesma parede. O que libera é apagar o alerta. */
    if(alertas.length){
      const lista = alertas.map(c => `<b>${esc(c)}</b>`).join(', ');
      const escolha = await confirmar('Este registro não pode ser apagado',
        `Ele gerou ${alertas.length === 1 ? 'o alerta' : 'os alertas'} ${lista},
         que ${alertas.length === 1 ? 'ficaria' : 'ficariam'} apontando para o vazio.
         <br><br><b>Apagar tudo</b> remove ${alertas.length === 1 ? 'o alerta' : 'os alertas'}
         e o registro de uma vez — é o que limpa lixo do detector.
         <br><br><b>Falso positivo</b> preserva o registro, tira da conta de acertos
         e alimenta a reavaliação do limiar. Prefira esta quando a detecção
         existiu mas estava errada.`,
        {rotulo: 'Apagar tudo', perigo: true,
         exigeTexto: 'Motivo (vai para a trilha de auditoria)',
         secundario: podeRevisar ? 'Marcar falso positivo' : null});

      if(escolha === '__secundario__'){ await marcarFalsoPositivo(); return; }
      if(!escolha) return;
      try{
        for(const cod of alertas) await API.excluirAlerta(cod, escolha);
        await API.removerDeteccao(d.codigo, escolha);
        fecharModal();
        toast('Registro e alerta removidos',
              `${d.codigo} e ${alertas.length} alerta(s) — na trilha de auditoria.`, 'warn');
        irPara(APP.view);
      }catch(e){ erroToast(e); }
      return;
    }

    const motivo = await confirmar('Descartar registro',
      `O registro <b>${esc(d.codigo)}</b> e o seu recorte serão apagados em definitivo. ` +
      `Isto não se desfaz.<br><br>Se a detecção está certa mas você discorda dela, ` +
      `marque como <b>falso positivo</b>: assim ela permanece e alimenta a reavaliação do limiar.`,
      {rotulo:'Descartar', perigo:true, exigeTexto:'Motivo do descarte'});
    if(!motivo) return;
    try{
      await API.removerDeteccao(d.codigo, motivo);
      fecharModal();
      toast('Registro descartado', `${d.codigo} — removido e registrado na trilha de auditoria.`, 'warn');
      irPara(APP.view);
    }catch(e){ erroToast(e); }
  };

  if(podeRevisar){
    const revisar = async veredito => {
      try{
        await API.revisar(d.codigo, veredito);
        fecharModal();
        toast(veredito === 'Confirmado' ? 'Detecção confirmada' : 'Marcado como falso positivo',
          `${d.codigo} — registrado na trilha de auditoria.`,
          veredito === 'Confirmado' ? 'ok' : 'warn');
        irPara(APP.view);
      }catch(e){ erroToast(e); }
    };
    $('#detConf').onclick  = () => revisar('Confirmado');
    const bf = $('#detFalso'); if(bf) bf.onclick = () => revisar('Falso positivo');
  }
}

/* ==========================================================
   BASE BIOMÉTRICA
   ========================================================== */
const ROTULO_SEXO = {M:'Masculino', F:'Feminino', N:'Não informado'};

/* Capturas de rosto sem cadastro que esperam virar BIO-N. Sem esta linha o
   cartão dizia "2 registros" com milhares de desconhecidos na base, e quem lia
   concluía que não havia ninguém por identificar — quando o que havia era
   ninguém triado ainda. O número já vinha do servidor e não era mostrado. */
function linhaTriagem(n){
  if(!n) return '';
  return `<span class="triagem" data-triagem role="link" tabindex="0"
            title="Abrir as capturas não identificadas no Reconhecimento facial">
    ${ico('face', 12)} ${fmtNum(n)} ${n === 1 ? 'captura aguarda' : 'capturas aguardam'} triagem</span>`;
}

/* A linha de triagem leva às capturas não identificadas. */
function ligarTriagem(raiz){
  $$('[data-triagem]', raiz).forEach(el => {
    const ir = e => {
      e.stopPropagation();
      if(!API.pode('facial')) return;
      FILTRO.facial.match = 'false'; PAG.facial = 1; irPara('facial');
    };
    el.onclick = ir;
    el.onkeydown = e => { if(e.key === 'Enter' || e.key === ' ') ir(e); };
  });
}

async function renderBiometria(alvo){
  const esquema = await API.esquemaIds();
  const pref = esquema.prefixos || {};
  const b = FILTRO.bio;
  const COR_SEXO = {M:'#35A7DE', F:'#E05F9E', N:'#7C8FA3'};
  const totalFichas = ['M','F','N'].reduce((s, k) => s + ((pref[k] || {}).registros || 0), 0);

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Base biométrica</h2><span class="cm-sub">registros com identificador permanente</span><span class="sp"></span>
    <div class="toolbar">
      ${API.pode('edit') ? `<button class="btn btn-sm bio-aprovar" id="bioSug" hidden>${ico('face',14)} Rostos para confirmar <b id="bioSugN"></b></button>` : ''}
      ${API.pode('edit') ? `<button class="btn btn-sm bio-aprovar" id="bioNovas" hidden>${ico('users',14)} Pessoas novas para aprovar <b id="bioNovasN"></b></button>` : ''}
      ${API.pode('edit') ? `<button class="btn btn-primary btn-sm" id="bioNovo">${ico('plus',14)} Novo registro</button>` : ''}
    </div></div>

  <div class="dash-grid dash-kpis">
    <div class="kpi bio-kpi">
      <div class="kpi-top"><span class="kpi-lbl">Fichas cadastradas</span><span class="kpi-ico">${ico('db',15)}</span></div>
      <div class="kpi-val">${fmtNum(totalFichas)}</div>
      <div class="cm-cob">${['M','F','N'].map(s => `<i style="width:${totalFichas ? ((pref[s]||{}).registros||0) / totalFichas * 100 : 0}%;max-width:none;background:${COR_SEXO[s]}"></i>`).join('')}</div>
      <div class="kpi-delta nt">${['M','F','N'].map(s => `${esc((pref[s]||{}).prefixo || 'BIO-'+s)} ${fmtNum((pref[s]||{}).registros||0)}`).join(' · ')}</div>
    </div>
    ${['M','F','N'].map(s => {
      const p = pref[s] || {};
      /* O cartão filtra a tabela por este prefixo — clicar em BIO-N lista
         todos os BIO-N. Clicar de novo no mesmo volta a mostrar todos. */
      return `<button type="button" class="kpi bio-kpi bio-pfx ${b.sexo===s?'ativo':''}" data-pfx="${s}" aria-pressed="${b.sexo===s}"
                style="--sx:${COR_SEXO[s]}"
                title="${b.sexo===s ? 'Mostrar todos os registros' : `Listar os registros ${esc(p.prefixo || 'BIO-'+s)}`}">
        <div class="kpi-top"><span class="kpi-lbl">${esc(p.rotulo || ROTULO_SEXO[s])}</span><span class="bio-pfx-tag mono">${esc(p.prefixo || 'BIO-'+s)}</span></div>
        <div class="kpi-val">${fmtNum(p.registros||0)}<small> registros</small></div>
        <div class="kpi-delta nt">próximo ID: <b class="mono" style="color:${COR_SEXO[s]}">${esc(p.proximo||'—')}</b></div>
        ${s === 'N' ? linhaTriagem(esquema.aguardando_triagem) : ''}
      </button>`;
    }).join('')}
  </div>

  <div class="cm-barra fc-barra">
    <div class="search">${ico('search',14)}<input class="inp" id="bioQ" value="${esc(b.q)}" placeholder="Nome, ID, CPF ou restrição…"></div>
    <div class="segm" id="bioSexoSeg">${[['','Todos', totalFichas],['M','Masculino',(pref.M||{}).registros||0],['F','Feminino',(pref.F||{}).registros||0],['N','Não informado',(pref.N||{}).registros||0]].map(([k,l,q]) =>
      `<button data-s="${k}" class="${b.sexo === k ? 'on' : ''}">${l} <em>${fmtNum(q)}</em></button>`).join('')}</div>
    <select class="inp" id="bioSexo" hidden><option value="">Todos os sexos</option>
      ${['M','F','N'].map(s => `<option value="${s}" ${b.sexo===s?'selected':''}>${ROTULO_SEXO[s]}</option>`).join('')}</select>
    <select class="inp" id="bioStatus"><option value="">Todas as situações</option>
      ${Object.entries(STATUS_PESSOA).map(([k,v]) => `<option value="${k}" ${b.status===k?'selected':''}>${esc(v.lbl)}</option>`).join('')}</select>
    <span class="sp"></span>
    <span class="cm-cont" id="bioCont"></span>
  </div>
  <p class="fc-nota" title="${esc(esquema.regra || '')}">${ico('lock',13)}<span><b>Regra de integridade:</b> o identificador é emitido uma única vez, no cadastro, e não muda; o sexo vem do registro civil, nunca da imagem.</span></p>

  <div class="panel"><div class="panel-bd tight"><div id="bioCorpo">${carregando()}</div></div></div>`;

  if(API.pode('edit')) API.sugestoes().then(r => {
    const b = $('#bioSug');
    if(!b || !r.total) return;
    $('#bioSugN').textContent = fmtNum(r.total);
    b.hidden = false;
    b.onclick = () => abrirSugestoes();
  }).catch(() => {});    // servidor sem a rota ainda: o botão só não aparece
  if(API.pode('edit')) API.triagemNovas().then(r => {
    const b = $('#bioNovas');
    if(!b || !r.total) return;
    $('#bioNovasN').textContent = fmtNum(r.total);
    b.hidden = false;
    b.onclick = () => abrirAprovacaoTriagem(r.grupos, 0);
  }).catch(() => {});

  const carregar = async () => {
    const corpo = $('#bioCorpo');
    corpo.innerHTML = carregando();
    FILTRO.bio = {q: $('#bioQ').value.trim(), sexo: $('#bioSexo').value, status: $('#bioStatus').value};
    try{
      const r = await API.pessoas({
        pagina: PAG.bio, por_pagina: POR_PAGINA,
        q: FILTRO.bio.q || undefined,
        sexo: FILTRO.bio.sexo || undefined,
        status: FILTRO.bio.status || undefined
      });
      const cont = $('#bioCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} ficha(s)`;
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl">
        <thead><tr><th>Pessoa</th><th>ID biométrico</th><th>Situação</th>
          <th style="width:200px">Qualidade do cadastro</th><th>Detecções</th><th>Última captura</th></tr></thead>
        <tbody>${(r.itens||[]).map(p => {
          const st = STATUS_PESSOA[p.status] || {};
          const cor = COR_SEXO[p.sexo];
          const q = p.qualidade || 0, qcor = q > .85 ? 'var(--green)' : q > .75 ? 'var(--amber)' : 'var(--red)';
          return `<tr class="clickable" data-p="${esc(p.id_biometrico)}">
            <td><div class="fc-pessoa"><canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="38" height="38"></canvas>
              <div><b>${esc(p.nome)}</b>${p.alcunha ? `<span class="mono">${esc(p.alcunha)}</span>` : ''}</div></div></td>
            <td><b class="mono bio-id" style="color:${cor}">${esc(p.id_biometrico)}</b><span class="fc-sub">${esc(ROTULO_SEXO[p.sexo])}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span></td>
            <td><div class="fc-sim"><div class="fc-sim-trilho"><i style="width:${(q * 100).toFixed(1)}%;background:${qcor}"></i></div>
                <b class="mono">${pct100(q)}</b></div><span class="fc-sub">${fmtNum(p.amostras)} amostra(s)</span></td>
            <td><b class="mono bio-det">${fmtNum(p.total_deteccoes)}</b></td>
            <td>${p.ultima_deteccao ? `<span class="fc-hora mono">${fmtDT(p.ultima_deteccao)}</span>
                <span class="fc-sub">${esc((p.ultima_camera||'').slice(0,22))} · ${relTime(p.ultima_deteccao)}</span>` : '<span class="fc-sub">—</span>'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="6">${vazio('db','Nenhum registro encontrado.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'bio')}`;
      pintarFaces(corpo);
      $$('tr[data-p]', corpo).forEach(t => t.onclick = () => abrirPessoa(t.dataset.p));
      ligarPager(corpo, 'bio', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  /* Prefixo, botão de sexo e o seletor escondido andam juntos. */
  const marcarPrefixo = s => {
    $$('[data-pfx]', alvo).forEach(c => {
      c.classList.toggle('ativo', c.dataset.pfx === s);
      c.setAttribute('aria-pressed', String(c.dataset.pfx === s));
    });
    $$('#bioSexoSeg button').forEach(x => x.classList.toggle('on', x.dataset.s === s));
  };
  const porSexo = s => { $('#bioSexo').value = s; PAG.bio = 1; marcarPrefixo(s); carregar(); };
  $('#bioQ').oninput = debounce(() => { PAG.bio = 1; carregar(); }, 350);
  $('#bioStatus').onchange = () => { PAG.bio = 1; carregar(); };
  $$('#bioSexoSeg button').forEach(x => x.onclick = () => porSexo(x.dataset.s));
  $$('[data-pfx]', alvo).forEach(c => c.onclick = e => {
    if(e.target.closest('[data-triagem]')) return;
    porSexo($('#bioSexo').value === c.dataset.pfx ? '' : c.dataset.pfx);
  });
  ligarTriagem(alvo);
  const novo = $('#bioNovo'); if(novo) novo.onclick = () => editarPessoa(null);

  await carregar();
}

/* A trilha da pessoa pelo parque.

   Mostra passagens, e não capturas: quem fica diante da câmera gera uma
   captura por volta do worker — no histórico desta base, mil numa câmera só.
   O que responde "por onde ela andou" é "esteve na Recepção das 09:12 às
   09:47", e é isso que o servidor já entrega agrupado. */
function trilhaHTML(t){
  if(!t) return '<p class="hint">A trajetória não carregou. O restante da ficha está completo; reabra para tentar de novo.</p>';
  if(!t.passagens.length) return '<p class="hint">Nenhuma passagem registrada para este identificador.</p>';

  const r = t.resumo;
  // Uma pessoa vista numa câmera só não tem trajetória — tem um ponto. Dizer
  // isso é mais útil que desenhar uma linha de um item e deixar quem lê
  // concluir que o sistema não procurou nas outras.
  const aviso = r.cameras < 2
    ? `<div class="note" style="margin-bottom:11px">${ico('info',15)}<div>
         Esta pessoa só foi reconhecida em <b>uma câmera</b>. Enquanto o
         template não tiver uma amostra do ângulo das demais, ela continua
         passando por elas sem ser identificada — a fila de conferência é
         onde isso se resolve.</div></div>`
    : '';

  // Onde esteve, antes de quando. A lista de passagens é longa e vem da mais
  // recente: uma câmera visitada uma única vez fica enterrada nela, e era o
  // que fazia a ficha dizer "2 câmeras" com a lista aparentando mostrar uma.
  const porCamera = (t.por_camera || []);
  // Só as câmeras: cada uma com quantos rostos o sistema captou ali e em
  // quantas passagens. Clicar abre os rostos para conferência.
  const totRostos = porCamera.reduce((a, c) => a + (c.rostos || 0), 0) || 1;
  return aviso + `
    <div class="trilha-resumo">
      <span><b>${fmtNum(r.cameras)}</b> câmera(s)</span>
      <span title="Quadros em que o rosto foi lido e identificado"><b>${fmtNum(r.rostos || 0)}</b> rostos captados</span>
      <span><b>${fmtNum(r.passagens)}</b> passagens</span>
    </div>
    ${porCamera.length ? `<div class="eyebrow" style="margin:12px 0 7px">Por câmera · clique para conferir os rostos</div>
      <div class="trilha-locais">
      ${porCamera.map((c, i) => `<button type="button" class="trilha-local tl-clic" data-tcam="${i}" title="Ver e conferir os rostos desta pessoa nesta câmera">
        <span class="tl-ico">${ico('cam', 14)}</span>
        <div class="trilha-local-bd">
          <b>${esc(c.camera)}</b>
          <span class="sub">${esc(c.setor && c.setor !== c.camera ? c.setor + ' · ' : '')}${esc(c.camera_codigo)} · ${fmtDate(c.primeira)}${fmtDate(c.primeira) !== fmtDate(c.ultima) ? ` → ${fmtDate(c.ultima)}` : ''}</span>
          <span class="tl-barra"><i style="width:${Math.max(2, (c.rostos || 0) / totRostos * 100).toFixed(1)}%"></i></span>
        </div>
        <div class="tl-num tl-rostos"><b class="mono">${fmtNum(c.rostos || 0)}</b><span>rosto${(c.rostos || 0) === 1 ? '' : 's'}</span></div>
        <div class="tl-num"><b class="mono">${fmtNum(c.passagens)}</b><span>passage${c.passagens === 1 ? 'm' : 'ns'}</span></div>
        <span class="tl-ver">Ver rostos ${ico('chev', 13)}</span>
      </button>`).join('')}
    </div>` : ''}`;
}

/* Os quadros em que a pessoa apareceu de corpo, um por linha.

   Complementa a trajetória em vez de repeti-la: lá o agrupamento responde
   "por onde ela andou", aqui cada linha é um quadro, que é o que responde
   "mostre-me". Fica em sanfona própria porque a lista é longa e só interessa
   a quem foi atrás dela. */
function duracaoCurta(s){
  if(s < 60) return s + 's';
  if(s < 3600) return Math.round(s / 60) + 'min';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h}h${String(m).padStart(2, '0')}` : `${h}h`;
}


async function abrirPessoa(idBio){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  // A ficha é a pessoa; o histórico é complemento. Num `Promise.all`, uma
  // falha no histórico derrubava a ficha inteira — e, sem prazo, uma chamada
  // pendurada deixava o modal no "carregando" para sempre, que foi o que o
  // operador viu como "biométrico que não abre".
  let p, trajeto;
  const [rp, rt] = await Promise.allSettled([
    API.pessoa(idBio), API.trajetoriaDaPessoa(idBio),
  ]);
  if(rp.status !== 'fulfilled'){
    box.innerHTML = `<div class="modal-bd">${erroBloco(rp.reason)}</div>`;
    return;
  }
  p = rp.value;
  trajeto = rt.status === 'fulfilled' ? rt.value : null;   // null = não deu para carregar

  const st = STATUS_PESSOA[p.status] || {};
  const cor = {M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo];
  const sexoMudou = p.sexo !== p.sexo_emissao;
  // A ficha abre com a trajetória fechada. O cadastro é o que se vem ver; a
  // trajetória é consulta, e aberta por padrão empurrava os dados da pessoa e
  // os botões para fora da tela. Fechada é também o estado em que a ficha
  // cabe inteira sem rolar.
  const trajetoriaAberta = false;

  box.className = 'modal wide pf-modal';
  const q = p.qualidade || 0, qcor = q > .85 ? 'var(--green)' : q > .75 ? 'var(--amber)' : 'var(--red)';
  const dado = (rot, val, mono) => `<div class="pf-kv"><span>${rot}</span><b class="${mono ? 'mono' : ''}">${val}</b></div>`;
  const tile = (rot, val, sub, extra) => `<div class="pf-tile"><span>${rot}</span><b>${val}</b>${extra || ''}<em>${sub}</em></div>`;
  box.innerHTML = `
    <div class="modal-hd pf-hd">
      <div class="pf-hd-t"><em>Ficha biométrica</em>
        <h3>${esc(p.nome)}${p.alcunha ? ` <small>${esc(p.alcunha)}</small>` : ''}</h3>
        <div class="cs-hd-meta">
          <span class="mono" style="color:${cor};font-weight:600">${esc(p.id_biometrico)}</span>
          <span>${esc(ROTULO_SEXO[p.sexo])}</span>
          <span><span class="chip ${st.cls}">${esc(st.lbl)}</span></span>
          ${p.unidade ? `<span>${ico('pin', 12)}${esc(p.unidade)}</span>` : ''}
        </div></div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd pf-bd">
      <div class="pf-topo">
        <div class="pf-foto" style="--sx:${cor}"><canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="132" height="132"></canvas></div>
        <div class="pf-tiles">
          ${tile('Qualidade do template', pct100(q), `${fmtNum(p.amostras)} amostra(s) facial(is)`,
            `<div class="kpi-barra"><i style="width:${(q * 100).toFixed(1)}%;background:${qcor}"></i></div>`)}
          ${trajeto ? tile('Detecções de rosto', fmtNum(trajeto.resumo.rostos || 0), `${fmtNum(trajeto.resumo.cameras)} câmera(s)`)
                    : tile('Detecções de rosto', '—', 'trajetória indisponível')}
          ${tile('Última captura', p.ultima_deteccao ? relTime(p.ultima_deteccao) : '—', p.ultima_deteccao ? fmtDT(p.ultima_deteccao) : 'nunca capturada')}
          ${tile('Cadastrado em', fmtDate(p.criado_em), 'identificador permanente desde então')}
        </div>
      </div>
      ${sexoMudou ? `<div class="note" style="margin:0 0 14px">${ico('info',15)}<div>O sexo deste cadastro foi corrigido para <b>${esc(ROTULO_SEXO[p.sexo])}</b> após a emissão. O identificador segue <b>${esc(p.id_biometrico)}</b> — o prefixo registra o que constava no ato da emissão, e reemitir quebraria o vínculo com as capturas já registradas.</div></div>` : ''}
      <div class="pf-cards">
        <div class="pf-card"><div class="pf-card-t">${ico('users', 13)} Dados cadastrais</div>
          ${dado('Nascimento', p.nascimento ? fmtDate(p.nascimento) : '—', true)}
          ${dado('CPF', esc(p.cpf || '—'), true)}
          ${dado('RG', esc(p.rg || '—'), true)}
          ${dado('Setor', esc(p.unidade || '—'))}
          ${p.restricao ? `${dado('Restrição', `<span style="color:var(--red)">${esc(p.restricao)}</span>`, true)}
            ${dado('Tipificação', esc(p.tipificacao || '—'))}${dado('Órgão', esc(p.orgao_expedidor || '—'))}` : ''}
        </div>
        <div class="pf-card"><div class="pf-card-t">${ico('shield', 13)} Registro</div>
          ${dado('Situação', esc(st.lbl || '—'))}
          ${dado('Identificador', `<span style="color:${cor}">${esc(p.id_biometrico)}</span>`, true)}
          ${dado('Emitido em', fmtDate(p.criado_em), true)}
          ${dado('Última câmera', esc(p.ultima_camera || '—'))}
        </div>
      </div>
      ${st.desc ? `<p class="fc-nota cf-nota">${ico('info',13)}<span>${esc(st.desc)}.</span></p>` : ''}
      <div class="sanfona ${trajetoriaAberta ? 'aberta' : ''}" id="pSanfonaTraj">
        <button type="button" class="sanfona-hd" id="pDobraTraj"
                aria-expanded="${trajetoriaAberta ? 'true' : 'false'}" aria-controls="pTraj"
                title="Por onde esta pessoa passou, agrupado em passagens.">
          <span class="chev">${ico('chev', 14)}</span>
          <b>Trajetória</b>
          <span class="chip c-gray mono">${trajeto
            ? `${trajeto.resumo.cameras} câmera(s)` : '—'}</span>
        </button>
        <div class="sanfona-bd" id="pTraj">${trilhaHTML(trajeto)}</div>
      </div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost btn-sm" data-close>Fechar</button>
      ${API.pode('edit') ? `<button class="btn btn-soft btn-sm" id="pAmostra">${ico('snap',14)} Adicionar foto</button>
                            <button class="btn btn-primary btn-sm" id="pEdit">${ico('edit',14)} Editar cadastro</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  // Os dois, e não só os rostos: as passagens da trajetória trazem o recorte
  // da captura, e canvas por pintar fica no padrão do elemento — 300 por 150,
  // que arrebenta a linha da lista.
  pintarFaces(box); pintarSnaps(box);
  $$('[data-det]', box).forEach(r => r.onclick = () => { fecharModal(); abrirDeteccao(r.dataset.det); });
  $$('[data-tcam]', box).forEach(b => b.onclick = () => abrirRostosCamera(p, (trajeto.por_camera || [])[+b.dataset.tcam]));

  // Quem abre e fecha é a sanfona inteira; o cabeçalho só anuncia o estado.
  const sanfona = $('#pSanfonaTraj', box), cabecalho = $('#pDobraTraj', box);
  if(sanfona && cabecalho) cabecalho.onclick = () => {
    const abrir = cabecalho.getAttribute('aria-expanded') !== 'true';
    cabecalho.setAttribute('aria-expanded', abrir ? 'true' : 'false');
    sanfona.classList.toggle('aberta', abrir);
  };

  const be = $('#pEdit'); if(be) be.onclick = () => { fecharModal(); editarPessoa(p.id_biometrico); };
  const ba = $('#pAmostra'); if(ba) ba.onclick = () => enviarFoto(p.id_biometrico);
}

/* Os rostos de uma pessoa numa câmera, para conferência.

   Abre a partir da trajetória da ficha. Todos vêm marcados; o que for
   desmarcado não é esta pessoa e sai da ficha pelo mesmo caminho da revisão
   de detecção ("Falso positivo"): a captura deixa de contar para ela, a
   amostra que nasceu dela sai do template e a pessoa vai para a sugerida —
   o erro fica registrado para a reavaliação do limiar, e na auditoria. */
async function abrirRostosCamera(p, cam){
  const POR = 200;
  let pagina = 1, total = 0, itens = [];
  let fotoAtual = p.foto_deteccao || '';
  const fora = new Set();
  const pode = API.pode('edit');
  const box = abrirModal(`
    <div class="modal-hd pf-hd">
      <button class="btn btn-ghost btn-sm" id="rcVoltar">${ico('lt', 14)} Ficha</button>
      <div class="pf-hd-t"><em>Rostos na câmera</em>
        <h3>${esc(cam.camera)}</h3>
        <div class="cs-hd-meta"><span class="mono">${esc(cam.camera_codigo)}</span>${cam.setor ? `<span>${ico('pin', 12)}${esc(cam.setor)}</span>` : ''}
          <span>${esc(p.nome)}</span><span class="mono" style="color:${{M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo]}">${esc(p.id_biometrico)}</span></div></div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="rc-barra">
        <span class="hint" id="rcInfo">carregando…</span><span class="sp"></span>
        ${pode ? `<button class="btn btn-ghost btn-sm" id="rcTodos">Marcar todos</button>
        <button class="btn btn-ghost btn-sm" id="rcNenhum">Desmarcar todos</button>` : ''}
      </div>
      <p class="fc-nota cf-nota" style="margin:0 0 12px">${ico('info',13)}<span>Desmarque os rostos que <b>não</b> forem de ${esc(p.nome)}: saem da ficha e do template. A <b>★</b> escolhe a foto da ficha.</span></p>
      <div class="rc-grade" id="rcGrade">${carregando()}</div>
      <div style="text-align:center;margin-top:12px"><button class="btn btn-ghost btn-sm" id="rcMais" hidden>Carregar mais</button></div>
    </div>
    <div class="modal-ft">
      <span class="rc-cont" id="rcCont"></span><span class="sp" style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-close>Fechar</button>
      ${pode ? `<button class="btn btn-danger btn-sm" id="rcRemover" disabled>${ico('x',14)} Remover da ficha</button>` : ''}
    </div>`, true);
  box.classList.add('pf-modal');

  const img = d => `${API.base}/deteccoes/${encodeURIComponent(d.codigo)}/snapshot?token=${encodeURIComponent(API.access)}`;
  const atualizar = () => {
    const n = fora.size;
    $('#rcCont', box).innerHTML = n ? `<b style="color:var(--red)">${fmtNum(n)}</b> rosto(s) marcados como <b>não é esta pessoa</b>` : '';
    const b = $('#rcRemover', box);
    if(b){ b.disabled = !n; b.innerHTML = `${ico('x',14)} Remover ${n ? fmtNum(n) + ' ' : ''}da ficha`; }
  };
  const htmlFoto = d => `
      <button type="button" class="rc-foto ${fora.has(d.codigo) ? 'fora' : ''}" data-rc="${esc(d.codigo)}" ${pode ? '' : 'disabled'}
              title="${fmtDT(d.ocorrido_em)}${d.similaridade != null ? ` · similaridade ${pct100(d.similaridade)}` : ''}">
        <img src="${img(d)}" loading="lazy" alt="">
        <span class="rc-chk">${ico(fora.has(d.codigo) ? 'x' : 'check', 11)}</span>
        ${pode ? `<span class="rc-estrela ${fotoAtual === d.codigo ? 'on' : ''}" role="button" data-foto="${esc(d.codigo)}"
              title="${fotoAtual === d.codigo ? 'Foto da ficha' : 'Usar como foto da ficha'}">★</span>` : ''}
        <span class="rc-pe mono">${fmtDate(d.ocorrido_em).slice(0, 5)} ${fmtTime(d.ocorrido_em).slice(0, 5)}${d.similaridade != null ? `<b>${Math.round(d.similaridade * 100)}%</b>` : ''}</span>
      </button>`;
  const info = () => {
    $('#rcInfo', box).textContent = itens.length < total
      ? `carregando ${fmtNum(itens.length)} de ${fmtNum(total)} rostos…`
      : `${fmtNum(total)} rosto(s) nesta câmera, do mais recente`;
    $('#rcMais', box).hidden = true;
  };
  // Grade inteira só quando a seleção muda em bloco; cada lote novo entra no fim.
  const pintar = () => {
    info();
    $('#rcGrade', box).innerHTML = itens.length ? itens.map(htmlFoto).join('') : vazio('face', 'Nenhum rosto desta pessoa nesta câmera.');
    atualizar();
  };
  const acrescentar = novos => {
    const g = $('#rcGrade', box);
    if(g.querySelector('.loading, .vazio, .empty') || !g.querySelector('.rc-foto')) g.innerHTML = '';
    g.insertAdjacentHTML('beforeend', novos.map(htmlFoto).join(''));
    if(!itens.length) g.innerHTML = vazio('face', 'Nenhum rosto desta pessoa nesta câmera.');
    info(); atualizar();
  };
  // Um ouvinte só para a grade toda: os lotes chegam depois e já respondem.
  $('#rcGrade', box).addEventListener('click', async ev => {
    const estrela = ev.target.closest('[data-foto]');
    if(estrela){
      ev.stopPropagation();
      const cod = estrela.dataset.foto, novo = fotoAtual === cod ? '' : cod;
      try{
        await API.escolherFoto(p.id_biometrico, novo);
        fotoAtual = novo; p.foto_deteccao = novo; FACE_VER[p.id_biometrico] = Date.now();
        $$('[data-foto]', box).forEach(e => { const on = e.dataset.foto === fotoAtual;
          e.classList.toggle('on', on); e.title = on ? 'Foto da ficha' : 'Usar como foto da ficha'; });
        toast('Foto da ficha', novo ? 'Este rosto passou a ser a foto da ficha.' : 'A ficha volta a usar o rosto mais confiável.', 'ok');
      }catch(err){ erroToast(err); }
      return;
    }
    const b = ev.target.closest('[data-rc]');
    if(!b || b.disabled) return;
    const k = b.dataset.rc;
    if(fora.has(k)) fora.delete(k); else fora.add(k);
    b.classList.toggle('fora', fora.has(k));
    $('.rc-chk', b).innerHTML = ico(fora.has(k) ? 'x' : 'check', 11);
    atualizar();
  });
  // Todas as páginas, uma atrás da outra: a grade mostra cada rosto que o
  // sistema ligou à pessoa nesta câmera, sem depender de "Carregar mais".
  let geracao = 0;
  const carregar = async () => {
    const minha = ++geracao;
    try{
      // Primeiro a lista inteira (só texto, rápida), depois a grade: as fotos
      // ocupam as conexões do navegador, e buscar a página seguinte no meio
      // delas deixava a carga parada nos primeiros 200.
      const lote = [];
      // Caminho rápido: tudo numa chamada. Servidor anterior a ele recusa o
      // limite (422) e a tela segue pelas páginas da lista geral.
      try{
        const todos = await API.rostosNaCamera(p.id_biometrico, cam.camera_codigo);
        if(minha !== geracao || !document.body.contains(box)) return;
        if(Array.isArray(todos) && todos.every(d => d.camera_codigo === cam.camera_codigo)){
          itens = todos; total = todos.length; pintar(); return;
        }
      }catch(e){ if(e && e.status && e.status !== 422) throw e; }
      do{
        const r = await API.deteccoes({tipo: 'FACIAL', camera: cam.camera_codigo, id_biometrico: p.id_biometrico,
                                       pagina, por_pagina: POR});
        if(minha !== geracao || !document.body.contains(box)) return;
        total = r.total || 0; const novos = r.itens || []; lote.push(...novos);
        $('#rcInfo', box).textContent = `carregando ${fmtNum(lote.length)} de ${fmtNum(total)} rostos…`;
        if(!novos.length) break;
        pagina++;
      }while(lote.length < total);
      itens = lote;
      pintar();
    }catch(e){ $('#rcGrade', box).innerHTML = erroBloco(e); }
  };

  $('#rcVoltar', box).onclick = () => { fecharModal(); abrirPessoa(p.id_biometrico); };

  const todos = $('#rcTodos', box); if(todos) todos.onclick = () => { fora.clear(); pintar(); };
  const nenhum = $('#rcNenhum', box); if(nenhum) nenhum.onclick = () => { itens.forEach(d => fora.add(d.codigo)); pintar(); };
  const rem = $('#rcRemover', box);
  if(rem) rem.onclick = async () => {
    const lista = [...fora];
    // Confirmação aqui mesmo: o diálogo do sistema usaria esta janela e a grade se perderia.
    if(rem.dataset.armado !== '1'){
      rem.dataset.armado = '1';
      rem.innerHTML = `${ico('alert',14)} Confirmar: remover ${fmtNum(lista.length)}`;
      $('#rcCont', box).innerHTML = `<span style="color:var(--red)">${fmtNum(lista.length)} rosto(s) saem da ficha de ${esc(p.nome)} e do template. As capturas ficam no registro como falso positivo. Clique de novo para confirmar.</span>`;
      clearTimeout(rem._t); rem._t = setTimeout(() => { rem.dataset.armado = ''; atualizar(); }, 8000);
      return;
    }
    rem.dataset.armado = ''; clearTimeout(rem._t);
    rem.disabled = true;
    let feitos = 0, falhas = 0;
    for(const cod of lista){
      try{ await API.revisar(cod, 'Falso positivo', `Conferência na ficha ${p.id_biometrico}: não é a pessoa`); feitos++; }
      catch(e){ falhas++; }
      rem.innerHTML = `${ico('x',14)} Removendo ${feitos + falhas}/${lista.length}…`;
    }
    toast('Ficha atualizada', `${fmtNum(feitos)} rosto(s) removido(s) da ficha${falhas ? ` · ${falhas} falharam` : ''}.`, falhas ? 'warn' : 'ok', 6000);
    if(lista.includes(fotoAtual)){ fotoAtual = ''; p.foto_deteccao = ''; }
    FACE_VER[p.id_biometrico] = Date.now();      // a foto pode ter mudado sozinha
    fora.clear(); itens = []; pagina = 1;
    $('#rcGrade', box).innerHTML = carregando();
    await carregar();
  };
  await carregar();
}

/* Rostos sugeridos: capturas soltas que a conciliação acha que são de uma
   ficha, mas sem certeza para vincular sozinha. Uma ficha por vez; o que
   ficar marcado entra nela, o que for desmarcado é "não é esta pessoa". */
async function abrirSugestoes(){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let r;
  try{ r = await API.sugestoes(); }catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }
  const cor = s => ({M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[s] || 'var(--acc)');
  box.classList.add('pf-modal');
  box.innerHTML = `
    <div class="modal-hd pf-hd"><div class="pf-hd-t"><em>Rostos para confirmar</em>
        <h3>${fmtNum(r.total)} rosto(s) parecidos com fichas existentes</h3>
        <div class="cs-hd-meta"><span>${fmtNum(r.fichas.length)} ficha(s)</span><span>a IA não teve certeza para vincular sozinha</span></div></div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      ${r.fichas.length ? `<div class="sg-lista">${r.fichas.map((f, i) => `
        <button type="button" class="sg-ficha" data-sg="${i}" style="--sx:${cor(f.sexo)}">
          <canvas class="bioface" data-face="${esc(f.id_biometrico)}" data-sexo="${esc(f.sexo)}" width="46" height="46"></canvas>
          <div><b>${esc(f.nome)}</b><span class="mono" style="color:${cor(f.sexo)}">${esc(f.id_biometrico)}</span></div>
          <div class="tl-num tl-rostos"><b class="mono">${fmtNum(f.total)}</b><span>rostos</span></div>
          <span class="tl-ver">Conferir ${ico('chev', 13)}</span>
        </button>`).join('')}</div>`
        : vazio('check', 'Nenhum rosto aguardando confirmação.', 'Tudo o que parecia ser de alguém já foi decidido.')}
    </div>
    <div class="modal-ft"><span class="hint" style="margin:0">Confirmados entram na ficha e ensinam a IA; recusados não voltam a ser sugeridos para aquela pessoa.</span>
      <span class="sp" style="flex:1"></span><button class="btn btn-ghost btn-sm" data-close>Fechar</button></div>`;
  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  $$('[data-sg]', box).forEach(b => b.onclick = () => abrirSugestoesPessoa(r.fichas[+b.dataset.sg]));
}

async function abrirSugestoesPessoa(f){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  box.classList.add('pf-modal');
  let itens;
  try{ itens = await API.sugestoesDaPessoa(f.id_biometrico); }catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }
  const fora = new Set();
  const img = d => `${API.base}/deteccoes/${encodeURIComponent(d.codigo)}/snapshot?token=${encodeURIComponent(API.access)}`;
  const cor = ({M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[f.sexo]) || 'var(--acc)';
  box.innerHTML = `
    <div class="modal-hd pf-hd">
      <button class="btn btn-ghost btn-sm" id="sgVoltar">${ico('lt', 14)} Fichas</button>
      <canvas class="bioface" data-face="${esc(f.id_biometrico)}" data-sexo="${esc(f.sexo)}" width="52" height="52" style="border-radius:10px;border:2px solid ${cor}"></canvas>
      <div class="pf-hd-t"><em>Estes rostos são desta pessoa?</em><h3>${esc(f.nome)}</h3>
        <div class="cs-hd-meta"><span class="mono" style="color:${cor}">${esc(f.id_biometrico)}</span><span>${fmtNum(itens.length)} rosto(s) sugerido(s), do mais parecido</span></div></div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="rc-barra"><span class="hint" id="sgInfo"></span><span class="sp"></span>
        <button class="btn btn-ghost btn-sm" id="sgTodos">Marcar todos</button>
        <button class="btn btn-ghost btn-sm" id="sgNenhum">Desmarcar todos</button></div>
      <p class="fc-nota cf-nota" style="margin:0 0 12px">${ico('info',13)}<span>Deixe marcados os rostos de ${esc(f.nome)} e <b>desmarque</b> os que não forem. Os marcados entram na ficha; os desmarcados ficam registrados como "não é esta pessoa".</span></p>
      <div class="rc-grade" id="sgGrade">${itens.map(d => `
        <button type="button" class="rc-foto" data-rc="${esc(d.codigo)}" title="${esc(d.camera)} · ${fmtDT(d.ocorrido_em)} · similaridade ${pct100(d.similaridade)}">
          <img src="${img(d)}" loading="lazy" alt="">
          <span class="rc-chk">${ico('check', 11)}</span>
          <span class="rc-pe mono">${esc(d.camera_codigo)}<b>${Math.round((d.similaridade || 0) * 100)}%</b></span>
        </button>`).join('') || vazio('check', 'Nada pendente para esta ficha.')}</div>
    </div>
    <div class="modal-ft">
      <span class="rc-cont" id="sgCont"></span><span class="sp" style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-close>Fechar</button>
      <button class="btn btn-primary btn-sm" id="sgSalvar" ${itens.length ? '' : 'disabled'}>${ico('check',14)} Salvar decisão</button>
    </div>`;
  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  const atualizar = () => {
    const sim = itens.length - fora.size;
    $('#sgInfo', box).textContent = `${fmtNum(sim)} marcado(s) · ${fmtNum(fora.size)} desmarcado(s)`;
    $('#sgCont', box).innerHTML = `<b style="color:var(--green)">${fmtNum(sim)}</b> entram na ficha · <b style="color:var(--red)">${fmtNum(fora.size)}</b> não são esta pessoa`;
  };
  const pintarSel = () => $$('[data-rc]', box).forEach(b => {
    b.classList.toggle('fora', fora.has(b.dataset.rc));
    $('.rc-chk', b).innerHTML = ico(fora.has(b.dataset.rc) ? 'x' : 'check', 11);
  });
  $('#sgGrade', box).addEventListener('click', ev => {
    const b = ev.target.closest('[data-rc]'); if(!b) return;
    const k = b.dataset.rc; if(fora.has(k)) fora.delete(k); else fora.add(k);
    pintarSel(); atualizar();
  });
  $('#sgTodos', box).onclick = () => { fora.clear(); pintarSel(); atualizar(); };
  $('#sgNenhum', box).onclick = () => { itens.forEach(d => fora.add(d.codigo)); pintarSel(); atualizar(); };
  $('#sgVoltar', box).onclick = () => abrirSugestoes();
  $('#sgSalvar', box).onclick = async () => {
    const b = $('#sgSalvar', box); b.disabled = true; b.innerHTML = `${ico('check',14)} Salvando…`;
    try{
      const r = await API.decidirSugestoes(f.id_biometrico,
        itens.map(d => d.codigo).filter(c => !fora.has(c)), [...fora]);
      FACE_VER[f.id_biometrico] = Date.now();
      toast('Decisão salva', `${fmtNum(r.confirmadas)} rosto(s) entraram na ficha de ${f.nome}`
        + `${r.recusadas ? ` · ${fmtNum(r.recusadas)} recusado(s)` : ''}`
        + `${r.amostras_pedidas ? ` · os ${r.amostras_pedidas} melhores viram amostras do template` : ''}.`, 'ok', 7000);
      abrirSugestoes();
    }catch(e){
      // Sem resposta não quer dizer sem gravação: pergunta ao servidor o que
      // ainda está pendente antes de dizer qualquer coisa.
      if(e && e.rede){
        try{
          const resta = await API.sugestoesDaPessoa(f.id_biometrico);
          const pendentes = new Set(resta.map(d => d.codigo));
          const gravados = itens.filter(d => !pendentes.has(d.codigo)).length;
          if(gravados){
            toast('Decisão salva', `${fmtNum(gravados)} rosto(s) decididos na ficha de ${f.nome}`
              + `${pendentes.size ? ` · ${fmtNum(pendentes.size)} ainda pendente(s)` : ''}.`, 'ok', 7000);
            abrirSugestoes(); return;
          }
        }catch(e2){ /* segue para o erro original */ }
      }
      erroToast(e); b.disabled = false; b.innerHTML = `${ico('check',14)} Salvar decisão`;
    }
  };
  atualizar();
}

/** Upload de foto para extrair o template — a imagem em si não é guardada. */
function enviarFoto(idBio){
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = async () => {
    const arq = input.files[0];
    if(!arq) return;
    toast('Processando', 'Extraindo o template facial da imagem…', 'info', 3000);
    try{
      const r = await API.enviarAmostra(idBio, arq);
      toast('Amostra adicionada',
        `Qualidade ${pct100(r.qualidade_amostra)} · ${r.amostras} amostra(s) no template. ${r.observacao}`,
        'ok', 6500);
      fecharModal();
      abrirPessoa(idBio);
    }catch(e){
      erroToast(e, 'Não foi possível processar a imagem');
    }
  };
  input.click();
}

/* Anexa o recorte guardado de uma captura como amostra biométrica.

   O template sai do rosto que a câmera realmente viu, não de uma foto posada
   — é por isso que a pessoa passa a ser reconhecida nas próximas passagens. */
async function anexarCaptura(idBio, codigoDeteccao){
  const r = await fetch(`${API.base}/deteccoes/${encodeURIComponent(codigoDeteccao)}/snapshot`
                      + `?token=${encodeURIComponent(API.access)}`);
  if(!r.ok) throw new Error('o recorte desta captura não está guardado');
  const blob = await r.blob();
  return API.enviarAmostra(idBio, new File([blob], `${codigoDeteccao}.jpg`, {type:'image/jpeg'}));
}

async function editarPessoa(idBio, opcoes){
  const novo = !idBio;
  const deCaptura = (opcoes || {}).deteccao || null;
  let p = null;
  if(!novo){
    try{ p = await API.pessoa(idBio); }
    catch(e){ return erroToast(e); }
  }
  const sexoAtual = p ? p.sexo : 'M';
  // Mesmo prédio das câmeras. O setor já gravado entra na lista junto, para
  // não ser trocado em silêncio no primeiro salvamento.
  let setores = [];
  try{ setores = (await API.setores(true)).setores || []; }catch(e){}
  const listaSetores = [...new Set([...setores, ...(p && p.unidade ? [p.unidade] : [])])].sort();

  const secP = (titulo, icone, sub, corpo) => `<section class="cs-sec">
      <header><span class="cf-gico">${ico(icone, 14)}</span><div><b>${titulo}</b><span>${sub}</span></div></header>${corpo}</section>`;
  const caixaP = abrirModal(`
    <div class="modal-hd pf-hd">
      <div class="pf-hd-t"><em>${novo ? 'Novo registro biométrico' : 'Editar cadastro'}</em>
        <h3>${novo ? 'Emissão de identificador' : esc(p.nome)}</h3>
        ${novo ? '' : `<div class="cs-hd-meta"><span class="mono" style="color:${{M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo]};font-weight:600">${esc(p.id_biometrico)}</span>
          <span>${esc(ROTULO_SEXO[p.sexo])}</span>${p.unidade ? `<span>${ico('pin', 12)}${esc(p.unidade)}</span>` : ''}</div>`}</div>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd pe-bd">
      <div class="pe-id">
        <canvas id="pvFace" class="bioface" width="64" height="64"></canvas>
        <div>
          <div class="pe-id-t">Identificador ${novo ? 'a emitir' : 'permanente'}</div>
          <div class="mono" id="pvId" style="font-size:20px;font-weight:600;margin-top:2px">…</div>
          <div class="hint" id="pvHint" style="margin-top:3px"></div>
        </div>
      </div>
      ${secP('Identificação', 'users', 'nome civil e dados pessoais', `<div class="form-grid">
        <div class="field full"><label for="pNome">Nome completo</label>
          <input class="inp" id="pNome" value="${p?esc(p.nome):''}" placeholder="Nome civil conforme documento"></div>
        <div class="field"><label for="pSexo">Sexo (registro civil)</label>
          <select class="inp" id="pSexo">
            ${['M','F','N'].map(s => `<option value="${s}" ${sexoAtual===s?'selected':''}>${ROTULO_SEXO[s]}</option>`).join('')}
          </select>
          <span class="hint">${novo ? 'Define o prefixo do identificador. Escolha definitiva no momento da emissão.'
                                    : 'Alterar <b>não</b> muda o ID já emitido.'}</span></div>
        <div class="field"><label for="pNasc">Data de nascimento</label>
          <input class="inp" id="pNasc" type="date" value="${p&&p.nascimento?esc(String(p.nascimento).slice(0,10)):''}"></div>
        <div class="field full"><label for="pAlc">Alcunha <span class="pe-opc">opcional</span></label>
          <input class="inp" id="pAlc" value="${p?esc(p.alcunha||''):''}" placeholder="Como a pessoa é conhecida"></div>
      </div>`)}
      ${secP('Documentos', 'log', 'documentos de identificação civil', `<div class="form-grid">
        <div class="field"><label for="pCpf">CPF</label>
          <input class="inp mono" id="pCpf" value="${p?esc(p.cpf||''):''}" placeholder="000.000.000-00"></div>
        <div class="field"><label for="pRg">RG</label>
          <input class="inp mono" id="pRg" value="${p?esc(p.rg||''):''}" placeholder="00.000.000-0"></div>
      </div>`)}
      ${secP('Vínculo e situação', 'shield', 'onde a pessoa atua e como o sistema a trata', `<div class="form-grid">
        <div class="field"><label for="pUni">Setor responsável</label>
          <select class="inp" id="pUni">${listaSetores
            .map(s => `<option ${p&&p.unidade===s?'selected':''}>${esc(s)}</option>`).join('')}</select></div>
        <div class="field"><label for="pStatus">Situação cadastral</label>
          <select class="inp" id="pStatus">${Object.entries(STATUS_PESSOA)
            .map(([k,v]) => `<option value="${k}" ${p&&p.status===k?'selected':''} title="${esc(v.desc)}">${esc(v.lbl)}</option>`).join('')}</select>
          <span class="hint" id="pStatusDesc"></span></div>
        <div class="field full" id="restricaoBox" hidden>
          <label for="pRestricao">Motivo da restrição</label>
          <input class="inp" id="pRestricao" value="${p?esc(p.restricao||''):''}" placeholder="Ex.: desligamento com pendência de devolução de crachá">
          <span class="hint">Obrigatório para “Acesso restrito”. Aparece no alerta, para quem atender saber por quê.</span></div>
      </div>`)}
      ${secP('Observações', 'edit', 'informações complementares', `
        <textarea class="inp" id="pObs" rows="3" style="width:100%" placeholder="Informações complementares relevantes à identificação">${p?esc(p.observacoes||''):''}</textarea>`)}
      <p class="fc-nota cf-nota" title="O tratamento é registrado na trilha de auditoria com finalidade, base legal e responsável, como a LGPD exige.">${ico('shield',13)}<span>O cadastro guarda um <b>template vetorial irreversível</b> (512 dimensões), não a fotografia original.</span></p>
    </div>
    <div class="modal-ft">
      ${!novo ? `<button class="btn btn-danger btn-sm" id="pDel">${ico('trash',14)} Excluir registro</button>` : ''}
      <span style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-close>Cancelar</button>
      <button class="btn btn-primary btn-sm" id="pSave">${ico('check',14)} ${novo ? 'Emitir identificador' : 'Salvar alterações'}</button>
    </div>`, true);
  caixaP.classList.add('pf-modal', 'pe-modal');
  const descStatus = () => { const d = $('#pStatusDesc'); if(d) d.textContent = ((STATUS_PESSOA[$('#pStatus').value] || {}).desc || ''); };
  descStatus(); $('#pStatus').addEventListener('change', descStatus);

  const atualizarPreview = async () => {
    const sx = $('#pSexo').value;
    let idPrev = p ? p.id_biometrico : '…';
    if(novo){
      try{ idPrev = (await API.proximoId(sx)).proximo_id; }catch(e){ idPrev = 'BIO-' + sx + '-…'; }
    }
    $('#pvId').textContent = idPrev;
    $('#pvId').style.color = {M:'#35A7DE', F:'#E05F9E', N:'#9BB0C3'}[novo ? sx : p.sexo_emissao];
    $('#pvHint').innerHTML = novo
      ? 'Sequência independente por sexo — nenhum outro registro receberá este número.'
      : (sx !== p.sexo
          ? `Sexo do cadastro passará a <b>${esc(ROTULO_SEXO[sx])}</b>; o identificador <b>permanece ${esc(p.id_biometrico)}</b>.`
          : `Emitido em ${fmtDate(p.criado_em)} · ${fmtNum(p.total_deteccoes)} captura(s) vinculada(s).`);
    drawBioFace($('#pvFace'), hash32(idPrev), novo ? sx : p.sexo_emissao, 64);
  };
  const atualizarRestricao = () => { $('#restricaoBox').hidden = $('#pStatus').value !== 'RESTRITO'; };

  $('#pSexo').onchange = atualizarPreview;
  $('#pStatus').onchange = atualizarRestricao;
  await atualizarPreview();
  atualizarRestricao();

  const bd = $('#pDel');
  if(bd) bd.onclick = async () => {
    const capturas = p.total_deteccoes || 0;
    const motivo = await confirmar('Excluir registro biométrico',
      `Apagar <b>${esc(p.id_biometrico)}</b> — ${esc(p.nome)} — em definitivo?<br><br>` +
      `O cadastro, as ${p.amostras || 0} amostra(s) e o template são removidos do banco. ` +
      (capturas ? `As ${fmtNum(capturas)} captura(s) dela voltam a constar como <b>não identificadas</b> ` +
                  `e permanecem no histórico. ` : '') +
      `O identificador <b>${esc(p.id_biometrico)}</b> volta a ficar livre para reemissão.` +
      `<br><br>Isto não se desfaz.`,
      {rotulo:'Excluir em definitivo', perigo:true, exigeTexto:'Motivo da exclusão'});
    if(!motivo) return;
    try{
      const r = await API.excluirPessoa(p.id_biometrico, motivo);
      fecharModal();
      toast('Registro excluído',
        `${r.id_biometrico} apagado` +
        (r.capturas_desvinculadas ? ` · ${fmtNum(r.capturas_desvinculadas)} captura(s) desvinculada(s)` : '') +
        '.', 'warn');
      irPara('biometrics');
    }catch(e){ erroToast(e); }
  };

  $('#pSave').onclick = async () => {
    const dados = {
      nome: $('#pNome').value.trim(),
      sexo: $('#pSexo').value,
      status: $('#pStatus').value,
      alcunha: $('#pAlc').value.trim(),
      nascimento: $('#pNasc').value || null,
      cpf: $('#pCpf').value.trim() || null,
      rg: $('#pRg').value.trim() || null,
      restricao: $('#pRestricao').value.trim(),
      unidade: $('#pUni').value,
      observacoes: $('#pObs').value.trim()
    };
    if(dados.nome.length < 5){ toast('Nome inválido', 'Informe o nome civil completo.', 'err'); return; }

    const btn = $('#pSave');
    btn.disabled = true;
    try{
      if(novo){
        const criado = await API.criarPessoa(dados);
        let extra = '';
        if(deCaptura){
          // O cadastro já existe e vale por si; se o rosto não anexar, a
          // pessoa fica sem template — dizer isso é melhor que silenciar.
          try{
            const r = await anexarCaptura(criado.id_biometrico, deCaptura);
            extra = ` Rosto de ${deCaptura} anexado (qualidade ${pct100(r.qualidade_amostra)}).`;
          }catch(e){
            extra = ` Atenção: o rosto não pôde ser anexado — ${e.message}. Envie uma foto na ficha.`;
          }
        }
        fecharModal();
        toast('Identificador emitido',
          `${criado.id_biometrico} — ${criado.nome}.${extra}`, 'ok', 7000);
        irPara('biometrics');
        setTimeout(() => abrirPessoa(criado.id_biometrico), 400);
      }else{
        const sexoMudou = dados.sexo !== p.sexo;
        await API.salvarPessoa(p.id_biometrico, dados);
        fecharModal();
        toast('Cadastro atualizado',
          sexoMudou ? `${p.id_biometrico} mantido — o identificador não muda com a correção do sexo.`
                    : `${p.id_biometrico} — ${dados.nome}`, 'ok', 5500);
        irPara('biometrics');
      }
    }catch(e){ erroToast(e); }
    finally{ btn.disabled = false; }
  };
}


/* ---------- Aprovação das pessoas novas da triagem ----------
   Um grupo por vez. O sexo NÃO vem marcado: o motor nunca o deduz da imagem
   (medido, ele trocou mulheres por homens), então quem aprova escolhe. Foto
   desmarcada não entra na ficha e continua na triagem.

   Se o grupo é de alguém que já tem ficha, "Vincular" junta as capturas a
   ela em vez de criar outra. As sugestões são as fichas mais parecidas; a
   busca acha qualquer outra. Com "incluir rostos compatíveis", as capturas
   que se parecem com as amostras da ficha viram amostras também — é o que
   faz o sistema passar a reconhecer a pessoa naquele ângulo. */
function abrirAprovacaoTriagem(grupos, i){
  if(i >= grupos.length){
    fecharModal();
    toast('Aprovação concluída', 'Não há mais grupos nesta lista.', 'ok');
    irPara(APP.view);
    return;
  }
  const g = grupos[i];
  const marcadas = new Set(g.capturas.map(c => c.codigo));
  let sexo = null;
  // Grupo que lembra alguém com ficha: já abre com ela escolhida.
  let ficha = g.ja_cadastrado ? {id_biometrico: g.ja_cadastrado.id_biometrico, nome: g.ja_cadastrado.nome} : null;
  const box = abrirModal(`
    <div class="modal-hd"><h3>${g.ja_cadastrado ? 'Parece alguém já cadastrado' : 'Pessoa nova'} ${i + 1} de ${grupos.length}</h3>
      <span class="sp" style="flex:1"></span>
      <button class="x" id="apX">${ico('x',16)}</button></div>
    <div class="modal-bd">
      ${g.ja_cadastrado ? `<div class="note" style="margin:0 0 10px">${ico('users',15)}<div>Estas fotos se parecem com
        <b>${esc(g.ja_cadastrado.nome)}</b> (${esc(g.ja_cadastrado.id_biometrico)}, ${Math.round(g.ja_cadastrado.similaridade * 100)}% parecido).
        Confira e use <b>Vincular</b>; se não for a mesma pessoa, desmarque a ficha e crie uma nova.</div></div>` : ''}
      <p class="hint" style="margin:0 0 10px">${g.capturas.length} capturas · ${esc(g.cameras.join(', '))}.
        Desmarque as fotos que <b>não</b> forem desta pessoa.</p>
      <div id="apFotos" style="display:flex;flex-wrap:wrap;gap:6px;max-height:360px;overflow:auto">
        ${g.capturas.map(c => `<label class="ap-foto" data-cod="${esc(c.codigo)}" title="${esc(c.camera)} · ${fmtDT(c.ocorrido_em)}"
             style="position:relative;cursor:pointer;border:2px solid var(--acc);border-radius:6px;overflow:hidden;line-height:0">
          <img src="${API.base}/deteccoes/${encodeURIComponent(c.codigo)}/snapshot?token=${encodeURIComponent(API.access)}"
               style="height:96px;width:auto;display:block" loading="lazy">
          <input type="checkbox" checked style="position:absolute;top:4px;left:4px"></label>`).join('')}
      </div>
      <div class="ap-vinc">
        <div class="ap-vinc-hd"><b>Esta pessoa já tem ficha?</b>
          <span class="hint" style="margin:0">Escolha a ficha para vincular as fotos marcadas a ela, em vez de criar outra.</span></div>
        <div class="ap-vinc-busca"><input class="inp" id="apBusca" placeholder="Buscar outra ficha por nome ou ID"></div>
        <div class="ap-vinc-lista" id="apSug"><span class="hint">Procurando fichas parecidas…</span></div>
        <label class="ap-vinc-amostra"><input type="checkbox" id="apAmostras" checked>
          Incluir os rostos compatíveis como amostras da ficha (o sistema passa a reconhecê-la nesse ângulo)</label>
      </div>
      <div style="display:flex;gap:18px;align-items:end;flex-wrap:wrap;margin-top:14px">
        <div><div class="hint" style="margin:0 0 6px">Sexo <b style="color:var(--amber)">(obrigatório para ficha nova)</b></div>
          <div class="segm" id="apSexo"><button data-s="M">Masculino</button><button data-s="F">Feminino</button></div></div>
        <label style="flex:1;min-width:260px"><div class="hint" style="margin:0 0 6px">Nome (fictício)</div>
          <input class="inp" id="apNome" style="width:100%" placeholder="escolha o sexo primeiro"></label>
      </div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" id="apDesc">Descartar grupo</button>
      <button class="btn btn-ghost" id="apPular">Pular</button>
      <span class="sp" style="flex:1"></span>
      <button class="btn btn-soft" id="apVinc" disabled title="Escolha uma ficha acima">${ico('users',14)} Vincular à ficha</button>
      <button class="btn btn-primary" id="apOk" disabled>${ico('check',14)} Aprovar e criar ficha</button>
    </div>`, true);
  const atualizar = () => {
    $('#apOk', box).disabled = !sexo || marcadas.size < 3 || $('#apNome', box).value.trim().length < 5;
    const v = $('#apVinc', box);
    v.disabled = !ficha || marcadas.size < 1;
    v.innerHTML = `${ico('users',14)} ${ficha ? 'Vincular a ' + esc(ficha.nome.split(' ')[0]) : 'Vincular à ficha'}`;
  };
  const cartao = (p, nota) => `
    <button type="button" class="ap-ficha ${ficha && ficha.id_biometrico === p.id_biometrico ? 'on' : ''}"
            data-id="${esc(p.id_biometrico)}" data-nome="${esc(p.nome)}">
      <img src="${API.base}/pessoas/${encodeURIComponent(p.id_biometrico)}/face?token=${encodeURIComponent(API.access)}"
           alt="" onerror="this.style.visibility='hidden'">
      <span><b>${esc(p.nome)}</b><small>${esc(p.id_biometrico)}${nota != null ? ` · ${Math.round(nota * 100)}% parecido` : ''}</small></span>
    </button>`;
  const mostrar = (lista, vazio) => {
    const alvo = $('#apSug', box);
    alvo.innerHTML = lista.length ? lista.map(x => cartao(x, x.similaridade)).join('')
                                  : `<span class="hint">${vazio}</span>`;
    $$('.ap-ficha', alvo).forEach(b => b.onclick = () => {
      ficha = ficha && ficha.id_biometrico === b.dataset.id ? null : {id_biometrico: b.dataset.id, nome: b.dataset.nome};
      $$('.ap-ficha', alvo).forEach(x => x.classList.toggle('on', !!ficha && x.dataset.id === ficha.id_biometrico));
      atualizar();
    });
  };
  let sugestoes = [];
  API.triagemParecidas(g.chave)
    .then(r => { sugestoes = r.sugestoes || []; if(!$('#apBusca', box).value.trim()) mostrar(sugestoes, 'Nenhuma ficha com amostras para comparar.'); })
    .catch(() => mostrar([], 'Não foi possível buscar fichas parecidas; use a busca.'));
  let tBusca = null;
  $('#apBusca', box).oninput = () => {
    clearTimeout(tBusca);
    const q = $('#apBusca', box).value.trim();
    if(!q){ mostrar(sugestoes, 'Nenhuma ficha com amostras para comparar.'); return; }
    tBusca = setTimeout(async () => {
      try{
        const r = await API.pessoas({q, por_pagina: 8});
        mostrar((r.itens || []).map(p => ({id_biometrico: p.id_biometrico, nome: p.nome})), 'Nenhuma ficha com esse nome ou ID.');
      }catch(e){ erroToast(e); }
    }, 300);
  };
  $$('.ap-foto', box).forEach(l => {
    const cb = $('input', l);
    cb.onchange = () => {
      cb.checked ? marcadas.add(l.dataset.cod) : marcadas.delete(l.dataset.cod);
      l.style.borderColor = cb.checked ? 'var(--acc)' : 'var(--line)';
      l.style.opacity = cb.checked ? '1' : '.35';
      atualizar();
    };
  });
  $$('#apSexo button', box).forEach(b => b.onclick = () => {
    sexo = b.dataset.s;
    $$('#apSexo button', box).forEach(x => x.classList.toggle('on', x === b));
    $('#apNome', box).value = sexo === 'M' ? g.nome_m : g.nome_f;
    atualizar();
  });
  $('#apNome', box).oninput = atualizar;
  $('#apX', box).onclick = () => { fecharModal(); irPara(APP.view); };
  $('#apPular', box).onclick = () => abrirAprovacaoTriagem(grupos, i + 1);
  $('#apDesc', box).onclick = async () => {
    try{ await API.triagemDescartar(g.chave); abrirAprovacaoTriagem(grupos, i + 1); }
    catch(e){ erroToast(e); }
  };
  $('#apVinc', box).onclick = async () => {
    if(!ficha) return;
    $('#apVinc', box).disabled = true;
    try{
      const r = await API.triagemVincular({chave: g.chave, id_biometrico: ficha.id_biometrico,
                                           codigos: [...marcadas], incluir_amostras: $('#apAmostras', box).checked});
      toast('Vinculado à ficha', `${r.capturas} captura(s) em ${r.nome} — ${r.id_biometrico}` +
            (r.amostras_novas ? ` · ${r.amostras_novas} rosto(s) novo(s) como amostra.` : '.'), 'ok');
      abrirAprovacaoTriagem(grupos, i + 1);
    }catch(e){ erroToast(e); atualizar(); }
  };
  $('#apOk', box).onclick = async () => {
    $('#apOk', box).disabled = true;
    try{
      const r = await API.triagemAprovar({chave: g.chave, sexo, nome: $('#apNome', box).value.trim(),
                                          codigos: [...marcadas]});
      toast('Ficha criada', `${esc(r.nome)} — ${esc(r.id_biometrico)} · ${r.capturas} capturas na trajetória.`, 'ok');
      abrirAprovacaoTriagem(grupos, i + 1);
    }catch(e){ erroToast(e); atualizar(); }
  };
}
