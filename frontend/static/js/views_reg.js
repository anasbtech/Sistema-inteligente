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
async function renderFacial(alvo){
  const f = FILTRO.facial, hoje = diaISO(new Date());
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Registros de reconhecimento facial</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="fcQ" value="${esc(f.q)}" placeholder="ID biométrico…"></div>
      <select class="inp" id="fcMatch">
        <option value="">Todas as capturas</option>
        <option value="true" ${f.match==='true'?'selected':''}>Com correspondência</option>
        <option value="false" ${f.match==='false'?'selected':''}>Não identificadas</option>
      </select>
      <select class="inp" id="fcCam"><option value="">Todas as câmeras</option>${opcoesCameras(c => c.analitico_facial, f.cam)}</select>
      <div class="periodo">
        <label for="fcDe">Período</label>
        <input class="inp" id="fcDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || hoje)}">
        <span class="sep">até</span>
        <input class="inp" id="fcAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${hoje}">
        <button class="btn btn-ghost btn-sm" id="fcLimpar" ${f.de||f.ate?'':'hidden'}>Limpar</button>
      </div>
    </div></div>
  <div class="note" style="margin-bottom:13px">${ico('info',15)}<div>Cada correspondência positiva vincula a captura a um <b>identificador biométrico permanente</b> (BIO-M / BIO-F / BIO-N). O identificador não muda entre capturas, e o sexo vem do cadastro da pessoa — não de inferência sobre a imagem. Captura sem correspondência ainda não tem identificador: ela espera a <b>triagem</b>, na Base biométrica, onde vira um BIO-N — que passa a BIO-M ou BIO-F quando a pessoa for cadastrada.</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="fcCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#fcCorpo');
    corpo.innerHTML = carregando();
    FILTRO.facial = {q: $('#fcQ').value.trim(), match: $('#fcMatch').value, cam: $('#fcCam').value,
                     de: $('#fcDe').value, ate: $('#fcAte').value};
    try{
      const r = await API.deteccoes({
        tipo: 'FACIAL', pagina: PAG.facial, por_pagina: POR_PAGINA,
        camera: FILTRO.facial.cam || undefined,
        apenas_match: FILTRO.facial.match || undefined,
        id_biometrico: /^BIO-[MFN]-\d{6}$/i.test(FILTRO.facial.q) ? FILTRO.facial.q.toUpperCase() : undefined,
        desde: limiteDia(FILTRO.facial.de),
        ate: limiteDia(FILTRO.facial.ate, true)
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Pessoa identificada</th><th>ID biométrico</th>
          <th>Situação</th><th>Similaridade</th><th>Câmera</th><th>Data / hora</th><th>Revisão</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => {
          const p = d.pessoa, st = p ? (STATUS_PESSOA[p.status]||{}) : null;
          return `<tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td>${p ? `<div class="face-cell">
                <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="30" height="30"></canvas>
                <div class="face-meta"><b>${esc(p.nome)}</b><span>${esc({M:'Masculino',F:'Feminino',N:'Não informado'}[p.sexo])}</span></div></div>`
              : `<span style="color:var(--txt-3)">Não identificada</span><br>
                 <span class="td-ts">abaixo do limiar de ${Math.round((d.limiar_aplicado||0)*100)}%</span>`}</td>
            <td class="td-id">${p ? esc(p.id_biometrico)
              // Sem cadastro o identificador ainda não existe, e escrever um
              // aqui afirmaria o que não foi emitido. O que se mostra é para
              // onde esta captura vai: o prefixo esmaecido diz a faixa, e a
              // linha abaixo diz que falta alguém triar.
              : `<span style="color:var(--txt-4)">BIO-N</span><br>
                 <span class="td-ts">a emitir na triagem</span>`}</td>
            <td>${st ? `<span class="chip ${st.cls}">${esc(st.lbl)}</span>` : '<span class="chip c-gray">Desconhecida</span>'}</td>
            <td>${barraConfianca(d.similaridade, p ? 'var(--green)' : 'var(--amber)')}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,26))}</span></td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}<br><span style="color:var(--txt-4)">${relTime(d.ocorrido_em)}</span></td>
            <td>${d.revisado ? `<span class="chip c-green">${esc(d.veredito||'Revisado')}</span>` : '<span class="chip c-amber">Pendente</span>'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="8">${vazio('face','Nenhum registro encontrado.','Ajuste os filtros ou aguarde novas capturas.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'facial')}`;
      pintarFaces(corpo); pintarSnaps(corpo);
      $$('tr[data-det]', corpo).forEach(t => t.onclick = () => abrirDeteccao(t.dataset.det));
      ligarPager(corpo, 'facial', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => { PAG.facial = 1; $('#fcDe').max = FILTRO.facial.ate || hoje; $('#fcAte').min = FILTRO.facial.de; $('#fcLimpar').hidden = !(FILTRO.facial.de || FILTRO.facial.ate); carregar(); };
  $('#fcQ').oninput = debounce(() => { PAG.facial = 1; carregar(); }, 350);
  ['fcMatch','fcCam','fcDe','fcAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#fcLimpar').onclick = () => { $('#fcDe').value = ''; $('#fcAte').value = ''; aplicar(); };
  await carregar();
}

/* ==========================================================
   OBJETOS
   ========================================================== */
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
  <div class="sec-hd"><h2>Detecção de objetos</h2><span class="sp"></span>
    <div class="toolbar">
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
    </div></div>
  <div class="panel" style="margin-bottom:14px"><div class="panel-hd"><h3>Classes detectadas</h3>
      <span class="sub" id="obGraficoSub">${esc(rotuloPeriodo(o.de, o.ate))}</span></div>
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
      const visiveis = classes
        .map(([k]) => ({k: ROTULO_OBJ[k], v: (r.classes || {})[k] || 0,
                        cor: COR_CLASSE[k] || 'var(--acc)'}))
        .filter(c => c.v > 0)
        .sort((x, y) => y.v - x.v);
      /* Deitadas e ordenadas, com o número ao lado. Em pé, Pessoa (dezenas de
         milhares) achatava o resto: Mochila e Volume viravam um traço sem
         nome legível. Aqui cada classe tem a sua linha e o seu valor. */
      g.innerHTML = visiveis.length
        ? barrasH(visiveis, {w: 900, linha: 28, rotulo: 150, maxNome: 24, pad: {t: 6, r: 70, b: 6}})
        : vazio('box', 'Nenhuma detecção neste período.', 'Escolha outro intervalo no calendário.');
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
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Classe</th><th>Confiança</th><th>Câmera</th>
          <th>Placa</th><th>Data / hora</th><th>Severidade</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => `
          <tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td><b style="color:${COR_CLASSE[d.classe]||'var(--txt-1)'}">${esc(rotuloClasse(d))}</b><br><span class="td-id">${esc(d.codigo)}</span></td>
            <td>${barraConfianca(d.confianca, COR_CLASSE[d.classe]||'var(--acc)')}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td>${d.placa ? placaHTML(d, 'mini') : '—'}</td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}</td>
            <td><span class="chip ${CLS_SEV[d.severidade]}">${esc(SEVERIDADE[d.severidade])}</span></td>
          </tr>`).join('') || `<tr><td colspan="7">${vazio('box','Nenhuma detecção encontrada.',
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
  alvo.innerHTML = `
  <div class="sec-hd"><h2>Análise de ações e comportamento</h2><span class="sp"></span>
    <div class="toolbar">
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
    </div></div>
  <div class="split" style="margin-bottom:14px">
    <div class="panel"><div class="panel-hd"><h3>Ações por tipo</h3><span class="sub" id="acGraficoSub">${esc(rotuloPeriodo(a.de, a.ate))}</span></div>
      <div class="panel-bd" id="acGrafico">${carregando()}</div></div>
    <div class="panel"><div class="panel-hd"><h3>Modelo de análise</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Detecção</dt><dd>YOLO — pessoas, veículos e objetos em cada quadro</dd>
          <dt>Rastreio</dt><dd>por posição, com previsão de movimento entre quadros próximos</dd>
          <dt>Sinais</dt><dd>velocidade em alturas de corpo, aceleração, permanência, densidade, cruzamento de linha, contato entre pessoas</dd>
          <dt>Comportamentos</dt><dd>${disponiveis} de ${classes.length} com detector ativo</dd>
          <dt>Janela</dt><dd>até 90 posições por pessoa</dd>
        </dl>
        <div class="note" style="margin-top:14px">${ico('alert',15)}<div>Eventos comportamentais são <b>indícios</b>, não conclusões. Toda ação de severidade crítica exige confirmação humana antes do despacho de guarnição.</div></div>
      </div></div>
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
        .map(([k]) => ({k: ROTULO_ACAO_UI[k], v: (r.classes || {})[k] || 0,
                        cor:'var(--violet)'}))
        .filter(c => c.v > 0)
        .sort((x, y) => y.v - x.v);
      // Deitadas: em pé, num meio painel, os nomes das ações saíam com 7 px.
      g.innerHTML = visiveis.length
        ? barrasH(visiveis, {w: 460, linha: 24, rotulo: 170, maxNome: 30, pad: {t: 4, r: 44, b: 4}})
        : vazio('box', 'Nenhuma ação neste período.', 'Escolha outro intervalo no calendário.');
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
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:64px">Captura</th><th>Ação detectada</th><th>Confiança</th>
          <th>Duração</th><th>Envolvidos</th><th>Câmera</th><th>Data / hora</th></tr></thead>
        <tbody>${(r.itens||[]).map(d => `
          <tr class="clickable" data-det="${esc(d.codigo)}">
            <td><canvas data-snap="${snapAttr(d)}" width="54"></canvas></td>
            <td style="padding-top:13px"><div style="display:flex;gap:8px;align-items:center">
              <span class="sev sev-${d.severidade}" style="height:28px"></span>
              <span><b>${esc(rotuloClasse(d))}</b><br><span class="td-id">${esc(d.codigo)}</span></span></div></td>
            <td>${barraConfianca(d.confianca, 'var(--violet)')}</td>
            <td class="mono">${duracao(d.duracao_s)}</td>
            <td class="mono">${d.envolvidos || 1}</td>
            <td><b>${esc(d.camera?d.camera.codigo:'')}</b><br><span class="td-ts">${esc((d.camera?d.camera.nome:'').slice(0,24))}</span></td>
            <td class="td-ts">${fmtDT(d.ocorrido_em)}<br><span style="color:var(--txt-4)">${relTime(d.ocorrido_em)}</span></td>
          </tr>`).join('') || `<tr><td colspan="7">${vazio('run','Nenhum evento comportamental encontrado.')}</td></tr>`}
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

  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd">
      <h3>${esc(d.tipo === 'FACIAL' ? (p ? 'Identificação positiva' : 'Captura não identificada') : rotuloClasse(d))}</h3>
      <span class="chip ${d.tipo === 'FACIAL' ? 'c-blue' : d.tipo === 'OBJETO' ? 'c-green' : 'c-violet'}">${esc(d.codigo)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="split" style="gap:16px">
        <div>
          <div class="eyebrow" style="margin-bottom:7px">Quadro da detecção</div>
          <div id="detMold" style="text-align:center">
            <div style="border:1px solid var(--line);border-radius:var(--r-m);overflow:hidden;background:#000;display:inline-block;line-height:0;max-width:100%">
              <canvas id="detBig" style="display:block"></canvas>
            </div>
          </div>
          <p class="hint" style="margin-top:8px">Chave da evidência: <span class="mono">${esc(d.snapshot_key || '—')}</span></p>
        </div>
        <div>
          ${p ? `<div class="eyebrow" style="margin-bottom:7px">Registro biométrico vinculado</div>
          <div style="display:flex;gap:12px;align-items:center;padding:11px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-800);margin-bottom:13px">
            <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="56" height="56"></canvas>
            <div style="min-width:0">
              <b style="display:block;font-size:14px">${esc(p.nome)}</b>
              <span class="mono" style="font-size:12px;color:var(--acc-2)">${esc(p.id_biometrico)}</span>
              <div style="margin-top:5px"><span class="chip ${(STATUS_PESSOA[p.status]||{}).cls}">${esc((STATUS_PESSOA[p.status]||{}).lbl)}</span></div>
            </div></div>` : ''}
          <dl class="kv">
            <dt>Evento</dt><dd class="mono">${esc(d.codigo)}</dd>
            <dt>Data / hora</dt><dd class="mono">${fmtDT(d.ocorrido_em)}</dd>
            <dt>Câmera</dt><dd>${esc(d.camera?d.camera.nome:'')}<br><span class="mono" style="font-size:11px;color:var(--txt-3)">${esc(d.camera?d.camera.codigo:'')} · ${esc(d.camera?d.camera.setor:'')}</span></dd>
            ${d.tipo === 'FACIAL' ? `
              <dt>Similaridade</dt><dd class="mono"><b style="color:${p?'var(--green)':'var(--amber)'}">${pct100(d.similaridade)}</b> (limiar ${Math.round((d.limiar_aplicado||0)*100)}%)</dd>
              <dt>Qualidade da face</dt><dd class="mono">${pct100(d.qualidade_face)}</dd>
              <dt>Sexo (cadastro)</dt><dd>${p ? esc({M:'Masculino',F:'Feminino',N:'Não informado'}[p.sexo]) : '—'}</dd>
              <dt>Embedding</dt><dd class="mono" style="font-size:11px">512-D · distância cosseno</dd>`
            : d.tipo === 'OBJETO' ? `
              <dt>Classe</dt><dd>${esc(rotuloClasse(d))}</dd>
              <dt>Confiança</dt><dd class="mono"><b>${pct100(d.confianca)}</b> (limiar ${Math.round((d.limiar_aplicado||0)*100)}%)</dd>
              ${d.placa ? `<dt>Placa (OCR)</dt><dd>${placaHTML(d, 'grande')}</dd>` : ''}`
            : `
              <dt>Ação</dt><dd>${esc(rotuloClasse(d))}</dd>
              <dt>Confiança</dt><dd class="mono"><b>${pct100(d.confianca)}</b></dd>
              <dt>Duração</dt><dd class="mono">${duracao(d.duracao_s)}</dd>
              <dt>Envolvidos</dt><dd class="mono">${d.envolvidos || 1} pessoa(s)</dd>`}
            <dt>Severidade</dt><dd><span class="chip ${CLS_SEV[d.severidade]}">${esc(SEVERIDADE[d.severidade])}</span></dd>
            <dt>Revisão</dt><dd>${d.revisado ? `<span class="chip c-green">${esc(d.veredito||'Revisado')}</span>` : '<span class="chip c-amber">Pendente</span>'}</dd>
            ${d.hash_evidencia ? `<dt>Hash</dt><dd class="mono" style="font-size:10.5px;word-break:break-all;color:var(--txt-3)">${esc(d.hash_evidencia)}</dd>` : ''}
          </dl>
        </div>
      </div>
      ${naoIdentificada ? `<div class="note" style="margin-top:15px">${ico('alert',15)}<div>Similaridade abaixo do limiar da câmera. A captura <b>não</b> foi atribuída a nenhuma pessoa e não gera identificação. O recorte é descartado no prazo de retenção, salvo vinculação a inquérito.</div></div>` : ''}
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${podeDescartar ? `<button class="btn btn-ghost" id="detDel">${ico('trash',14)} Descartar</button>` : ''}
      ${p && API.pode('biometrics') ? `<button class="btn btn-soft" id="detPessoa">${ico('db',14)} Abrir ficha</button>` : ''}
      ${naoIdentificada && API.pode('biometrics') && API.pode('edit')
        ? `<button class="btn btn-soft" id="detVincular">${ico('face',14)} Vincular a cadastro</button>
           <button class="btn btn-soft" id="detCadastrar">${ico('db',14)} Cadastrar na base</button>` : ''}
      ${d.camera && d.camera.codigo !== 'VIDEO-TESTE' ? `<button class="btn btn-soft" id="detCam">${ico('live',14)} Ver câmera</button>` : ''}
      ${podeRevisar ? `<button class="btn btn-primary" id="detConf">${ico('check',14)} Confirmar</button>` : ''}
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

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Base biométrica</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="bioQ" value="${esc(b.q)}" placeholder="Nome, ID, CPF ou restrição…"></div>
      <select class="inp" id="bioSexo"><option value="">Todos os sexos</option>
        ${['M','F','N'].map(s => `<option value="${s}" ${b.sexo===s?'selected':''}>${ROTULO_SEXO[s]}</option>`).join('')}</select>
      <select class="inp" id="bioStatus"><option value="">Todas as situações</option>
        ${Object.entries(STATUS_PESSOA).map(([k,v]) => `<option value="${k}" ${b.status===k?'selected':''}>${esc(v.lbl)}</option>`).join('')}</select>
      ${API.pode('edit') ? `<button class="btn btn-soft btn-sm" id="bioNovas" hidden>${ico('users',14)} Pessoas novas para aprovar <b id="bioNovasN"></b></button>` : ''}
      ${API.pode('edit') ? `<button class="btn btn-primary btn-sm" id="bioNovo">${ico('plus',14)} Novo registro</button>` : ''}
    </div></div>

  <div class="panel" style="margin-bottom:14px">
    <div class="panel-hd"><h3>Esquema de identificadores</h3>
      <span class="sub">um prefixo por sexo · sequência independente · identificador permanente</span></div>
    <div class="panel-bd">
      <div class="id-scheme">
        ${['M','F','N'].map(s => {
          const p = pref[s] || {};
          const cor = {M:'var(--acc-2)', F:'#F08CBE', N:'var(--txt-2)'}[s];
          /* O cartão filtra a tabela por este prefixo — clicar em BIO-N lista
             todos os BIO-N. Clicar de novo no mesmo volta a mostrar todos. */
          return `<button type="button" class="id-card ${s.toLowerCase()} ${b.sexo===s?'ativo':''}"
                    data-pfx="${s}" aria-pressed="${b.sexo===s}"
                    title="${b.sexo===s ? 'Mostrar todos os registros' : `Listar os registros ${esc(p.prefixo || 'BIO-'+s)}`}">
            <div class="pfx">${esc(p.prefixo || 'BIO-'+s)}-000000</div>
            <div class="lbl">${esc(p.rotulo || ROTULO_SEXO[s])}</div>
            <div class="cnt">${fmtNum(p.registros||0)} registros · próximo: <b style="color:${cor}">${esc(p.proximo||'—')}</b></div>
            ${s === 'N' ? linhaTriagem(esquema.aguardando_triagem) : ''}
          </button>`;
        }).join('')}
      </div>
      <div class="note" style="margin-top:13px">${ico('lock',15)}<div><b>Regra de integridade:</b> ${esc(esquema.regra || '')}</div></div>
    </div></div>

  <div class="panel"><div class="panel-bd tight"><div id="bioCorpo">${carregando()}</div></div></div>`;

  if(API.pode('edit')) API.triagemNovas().then(r => {
    const b = $('#bioNovas');
    if(!b || !r.total) return;
    $('#bioNovasN').textContent = `(${r.total})`;
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
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:52px">Face</th><th>Nome</th><th>ID biométrico</th><th>Sexo</th>
          <th>Situação</th><th>Qualidade</th><th>Detecções</th><th>Última captura</th></tr></thead>
        <tbody>${(r.itens||[]).map(p => {
          const st = STATUS_PESSOA[p.status] || {};
          const cor = {M:'#35A7DE', F:'#E05F9E', N:'#5F7589'}[p.sexo];
          return `<tr class="clickable" data-p="${esc(p.id_biometrico)}">
            <td><canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="36" height="36"></canvas></td>
            <td><b>${esc(p.nome)}</b>${p.alcunha ? `<br><span class="td-ts">${esc(p.alcunha)}</span>` : ''}</td>
            <td class="td-id"><b style="color:${cor}">${esc(p.id_biometrico)}</b></td>
            <td><span class="chip" style="color:${cor};border-color:${cor}55;background:transparent">${esc(ROTULO_SEXO[p.sexo])}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span></td>
            <td>${barraConfianca(p.qualidade, p.qualidade > .85 ? 'var(--green)' : p.qualidade > .75 ? 'var(--amber)' : 'var(--red)')}
                <span class="td-ts">${p.amostras} amostra(s)</span></td>
            <td class="mono"><b>${fmtNum(p.total_deteccoes)}</b></td>
            <td class="td-ts">${p.ultima_deteccao ? fmtDT(p.ultima_deteccao) + '<br><span style="color:var(--txt-4)">' + esc((p.ultima_camera||'').slice(0,22)) + '</span>' : '—'}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="8">${vazio('db','Nenhum registro encontrado.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'bio')}`;
      pintarFaces(corpo);
      $$('tr[data-p]', corpo).forEach(t => t.onclick = () => abrirPessoa(t.dataset.p));
      ligarPager(corpo, 'bio', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#bioQ').oninput = debounce(() => { PAG.bio = 1; carregar(); }, 350);
  ['bioSexo','bioStatus'].forEach(id => $('#'+id).onchange = () => {
    PAG.bio = 1; marcarPrefixo($('#bioSexo').value); carregar();
  });
  /* Cartão do prefixo: filtra a tabela por ele, ou desfaz o filtro. */
  const marcarPrefixo = s => $$('[data-pfx]', alvo).forEach(c => {
    c.classList.toggle('ativo', c.dataset.pfx === s);
    c.setAttribute('aria-pressed', String(c.dataset.pfx === s));
  });
  $$('[data-pfx]', alvo).forEach(c => c.onclick = e => {
    if(e.target.closest('[data-triagem]')) return;
    const s = $('#bioSexo').value === c.dataset.pfx ? '' : c.dataset.pfx;
    $('#bioSexo').value = s; PAG.bio = 1; marcarPrefixo(s); carregar();
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
  return aviso + `
    <div class="trilha-resumo">
      <span><b>${fmtNum(r.passagens)}</b> passagens</span>
      <span><b>${fmtNum(r.cameras)}</b> câmeras</span>
      <span title="Quadros em que o rosto foi lido e identificado"><b>${fmtNum(r.rostos || 0)}</b> por rosto</span>
    </div>
    ${porCamera.length ? `<div class="trilha-locais">
      ${porCamera.map(c => `<div class="trilha-local">
        <div class="trilha-local-bd">
          <b>${esc(c.camera)}</b>
          <span class="sub">${esc(c.setor || '')} · ${esc(c.camera_codigo)}</span>
        </div>
        <div class="trilha-local-num">
          <span class="mono">${fmtNum(c.passagens)}</span>
          <span class="sub">passagem${c.passagens === 1 ? '' : 's'}</span>
        </div>
        <div class="trilha-local-num">
          <span class="mono" style="font-size:12px">${fmtNum(c.rostos || 0)}</span>
          <span class="sub">rostos</span>
        </div>
        <div class="trilha-local-quando">
          <span class="sub mono">${fmtDate(c.primeira)}</span>
          <span class="sub mono">${fmtDate(c.ultima)}</span>
        </div>
      </div>`).join('')}
    </div>` : ''}
    <div class="eyebrow" style="margin:14px 0 7px">Passagens, da mais recente</div>
    <div class="trilha rank-rolavel">
      ${t.passagens.map(p => `
        <div class="trilha-item clickable" data-det="${esc(p.codigo)}" style="cursor:pointer">
          <canvas data-snap="${snapAttr(p)}" width="42" height="53"></canvas>
          <div class="trilha-bd">
            <b>${esc(p.camera)}</b>
            <span class="sub">${esc(p.setor || '')} · ${esc(p.camera_codigo)}</span>
            <span class="sub mono">${fmtDT(p.inicio)} — ${fmtTime(p.fim)}</span>
          </div>
          <div class="trilha-num">
            <span class="mono">${duracaoCurta(p.duracao_s)}</span>
            <span class="sub mono">${fmtNum(p.rostos || 0)} rosto${(p.rostos || 0) === 1 ? '' : 's'}</span>
          </div>
        </div>`).join('')}
    </div>`;
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

  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd"><h3>Ficha biométrica</h3><span class="chip ${st.cls}">${esc(st.lbl)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:17px">
        <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="104" height="104" style="border-radius:var(--r-m)"></canvas>
        <div style="flex:1;min-width:210px">
          <b style="font-size:19px;display:block;font-family:var(--f-disp);letter-spacing:.01em">${esc(p.nome)}</b>
          ${p.alcunha ? `<span style="color:var(--txt-3);font-size:12.5px">${esc(p.alcunha)}</span>` : ''}
          <div style="margin:9px 0;display:flex;gap:7px;align-items:center;flex-wrap:wrap">
            <span class="mono" style="font-size:16px;font-weight:600;color:${cor}">${esc(p.id_biometrico)}</span>
            <span class="chip" style="color:${cor};border-color:${cor}55;background:transparent">${esc(ROTULO_SEXO[p.sexo])}</span>
          </div>
          <p class="hint" style="max-width:52ch">${esc(st.desc||'')}. Identificador emitido em ${fmtDate(p.criado_em)} e permanente desde então.</p>
        </div>
      </div>
      ${sexoMudou ? `<div class="note" style="margin-bottom:15px">${ico('info',15)}<div>O sexo deste cadastro foi corrigido para <b>${esc(ROTULO_SEXO[p.sexo])}</b> após a emissão. O identificador segue <b>${esc(p.id_biometrico)}</b> — o prefixo registra o que constava no ato da emissão, e reemitir quebraria o vínculo com as capturas já registradas.</div></div>` : ''}
      <div class="split" style="gap:16px">
        <dl class="kv">
          <dt>Nascimento</dt><dd class="mono">${p.nascimento ? fmtDate(p.nascimento) : '—'}</dd>
          <dt>CPF</dt><dd class="mono">${esc(p.cpf || '—')}</dd>
          <dt>RG</dt><dd class="mono">${esc(p.rg || '—')}</dd>
          <dt>Setor</dt><dd>${esc(p.unidade || '—')}</dd>
          ${p.restricao ? `<dt>Restrição</dt><dd class="mono" style="color:var(--red)">${esc(p.restricao)}</dd>
          <dt>Tipificação</dt><dd>${esc(p.tipificacao)}</dd>
          <dt>Órgão</dt><dd>${esc(p.orgao_expedidor)}</dd>` : ''}
        </dl>
        <dl class="kv">
          <dt>Qualidade do template</dt><dd class="mono">${pct100(p.qualidade)}</dd>
          <dt>Amostras faciais</dt><dd class="mono">${p.amostras}</dd>
          <dt>Total de detecções</dt><dd class="mono"><b>${fmtNum(p.total_deteccoes)}</b></dd>
          <dt>Última captura</dt><dd class="mono">${p.ultima_deteccao ? fmtDT(p.ultima_deteccao) : '—'}</dd>
          <dt>Cadastrado em</dt><dd class="mono">${fmtDate(p.criado_em)}</dd>
          <dt>Base legal</dt><dd>${esc(p.base_legal)}</dd>
        </dl>
      </div>
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
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${API.pode('edit') ? `<button class="btn btn-soft" id="pAmostra">${ico('snap',14)} Adicionar foto</button>
                            <button class="btn btn-primary" id="pEdit">${ico('edit',14)} Editar cadastro</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  // Os dois, e não só os rostos: as passagens da trajetória trazem o recorte
  // da captura, e canvas por pintar fica no padrão do elemento — 300 por 150,
  // que arrebenta a linha da lista.
  pintarFaces(box); pintarSnaps(box);
  $$('[data-det]', box).forEach(r => r.onclick = () => { fecharModal(); abrirDeteccao(r.dataset.det); });

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

  abrirModal(`
    <div class="modal-hd"><h3>${novo ? 'Novo registro biométrico' : 'Editar cadastro'}</h3>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:14px;align-items:center;padding:12px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-800);margin-bottom:16px">
        <canvas id="pvFace" class="bioface" width="64" height="64"></canvas>
        <div>
          <div class="eyebrow">Identificador ${novo ? 'a emitir' : 'permanente'}</div>
          <div class="mono" id="pvId" style="font-size:19px;font-weight:600;margin-top:3px">…</div>
          <div class="hint" id="pvHint" style="margin-top:3px"></div>
        </div>
      </div>
      <div class="form-grid">
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
        <div class="field"><label for="pCpf">CPF</label>
          <input class="inp mono" id="pCpf" value="${p?esc(p.cpf||''):''}" placeholder="000.000.000-00"></div>
        <div class="field"><label for="pRg">RG</label>
          <input class="inp mono" id="pRg" value="${p?esc(p.rg||''):''}" placeholder="00.000.000-0"></div>
        <div class="field"><label for="pAlc">Alcunha (opcional)</label>
          <input class="inp" id="pAlc" value="${p?esc(p.alcunha||''):''}"></div>
        <div class="field"><label for="pUni">Setor responsável</label>
          <select class="inp" id="pUni">${listaSetores
            .map(s => `<option ${p&&p.unidade===s?'selected':''}>${esc(s)}</option>`).join('')}</select></div>
        <div class="field full"><label for="pStatus">Situação cadastral</label>
          <select class="inp" id="pStatus">${Object.entries(STATUS_PESSOA)
            .map(([k,v]) => `<option value="${k}" ${p&&p.status===k?'selected':''}>${esc(v.lbl)} — ${esc(v.desc)}</option>`).join('')}</select></div>
        <div class="field full" id="restricaoBox" hidden>
          <label for="pRestricao">Motivo da restrição</label>
          <input class="inp" id="pRestricao" value="${p?esc(p.restricao||''):''}" placeholder="Ex.: desligamento com pendência de devolução de crachá">
          <span class="hint">Obrigatório para “Acesso restrito”. Aparece no alerta, para quem atender saber por quê.</span></div>
        <div class="field full"><label for="pObs">Observações</label>
          <textarea class="inp" id="pObs" placeholder="Informações complementares relevantes à identificação">${p?esc(p.observacoes||''):''}</textarea></div>
      </div>
      <div class="note" style="margin-top:15px">${ico('shield',15)}<div>O cadastro armazena um <b>template vetorial irreversível</b> (512 dimensões), não a fotografia original. O tratamento é registrado na trilha de auditoria com finalidade, base legal e responsável, como a LGPD exige.</div></div>
    </div>
    <div class="modal-ft">
      ${!novo ? `<button class="btn btn-danger" id="pDel">${ico('trash',14)} Excluir registro</button>` : ''}
      <span style="flex:1"></span>
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="pSave">${ico('check',14)} ${novo ? 'Emitir identificador' : 'Salvar alterações'}</button>
    </div>`);

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
   desmarcada não entra na ficha e continua na triagem. */
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
  const box = abrirModal(`
    <div class="modal-hd"><h3>Pessoa nova ${i + 1} de ${grupos.length}</h3>
      <span class="sp" style="flex:1"></span>
      <button class="x" id="apX">${ico('x',16)}</button></div>
    <div class="modal-bd">
      <p class="hint" style="margin:0 0 10px">${g.capturas.length} capturas · ${esc(g.cameras.join(', '))}.
        Desmarque as fotos que <b>não</b> forem desta pessoa.</p>
      <div id="apFotos" style="display:flex;flex-wrap:wrap;gap:6px;max-height:360px;overflow:auto">
        ${g.capturas.map(c => `<label class="ap-foto" data-cod="${esc(c.codigo)}" title="${esc(c.camera)} · ${fmtDT(c.ocorrido_em)}"
             style="position:relative;cursor:pointer;border:2px solid var(--acc);border-radius:6px;overflow:hidden;line-height:0">
          <img src="${API.base}/deteccoes/${encodeURIComponent(c.codigo)}/snapshot?token=${encodeURIComponent(API.access)}"
               style="height:96px;width:auto;display:block" loading="lazy">
          <input type="checkbox" checked style="position:absolute;top:4px;left:4px"></label>`).join('')}
      </div>
      <div style="display:flex;gap:18px;align-items:end;flex-wrap:wrap;margin-top:14px">
        <div><div class="hint" style="margin:0 0 6px">Sexo <b style="color:var(--amber)">(obrigatório)</b></div>
          <div class="segm" id="apSexo"><button data-s="M">Masculino</button><button data-s="F">Feminino</button></div></div>
        <label style="flex:1;min-width:260px"><div class="hint" style="margin:0 0 6px">Nome (fictício)</div>
          <input class="inp" id="apNome" style="width:100%" placeholder="escolha o sexo primeiro"></label>
      </div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" id="apDesc">Descartar grupo</button>
      <button class="btn btn-ghost" id="apPular">Pular</button>
      <span class="sp" style="flex:1"></span>
      <button class="btn btn-primary" id="apOk" disabled>${ico('check',14)} Aprovar e criar ficha</button>
    </div>`, true);
  const atualizar = () => {
    $('#apOk', box).disabled = !sexo || marcadas.size < 3 || $('#apNome', box).value.trim().length < 5;
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
