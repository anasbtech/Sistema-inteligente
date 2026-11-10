/* ===================== Inteligência veicular =====================
   À esquerda, a câmera ao vivo com as caixas do módulo veicular: número do
   rastreio, categoria, cor, marca/modelo e placa assim que identificados. À
   direita, os veículos em cena agora e o estado dos modelos. Embaixo, o
   histórico de passagens — uma linha por veículo, não por quadro.

   Atributo vazio é "não identificado", nunca palpite: a ficha diz por quê
   (modelo ainda não treinado, confiança abaixo do piso, imagem sem cor). É
   também na ficha que se rotula o veículo, e é com esses rótulos que cor e
   marca/modelo são treinados e medidos (scripts/veiculos_treinar.py). */

/* Espelha identificacao.TIPOS. `caminhao` fica para as passagens gravadas
   antes da separação por porte; `veiculo` é "o tipo não foi confirmado". */
const VEIC_ROT_CAT = {carro:'Carro', picape:'Picape', kombi:'Kombi', van:'Van / utilitário',
  caminhao_pequeno:'Caminhão pequeno', caminhao_grande:'Caminhão grande', onibus:'Ônibus',
  motocicleta:'Motocicleta', veiculo:'Veículo', caminhao:'Caminhão'};
const VEIC_TIPOS_ROTULO = ['carro', 'picape', 'kombi', 'van', 'caminhao_pequeno', 'caminhao_grande', 'onibus', 'motocicleta'];
const VEIC = {cam: null, filtro: {categoria:'', cor:'', placa:'', rotulagem:false, de:'', ate:''},
              pagina: 1, catalogo: null, emCena: []};

const veicPct = v => v == null ? '' : `${Math.round(v * 100)}%`;
const veicNaoId = motivo => `<span class="veic-nid" title="${esc(motivo || '')}">não identificado</span>`;

/* O que uma pessoa disse que o veículo é vale mais que o palpite da IA, e
   aparece como tal: "conferido". Quando a IA disse outra coisa, isso fica à
   vista — é exatamente o erro que o treino precisa corrigir. */
const veicConferido = '<span class="veic-conf-tag" title="Rotulado por uma pessoa na ficha">conferido</span>';
const veicIaDisse = (txt, conf) => `<div class="veic-sub">IA: ${esc(txt)} ${veicPct(conf)}</div>`;

function veicTipo(p){
  const r = p.rotulo || {};
  const nome = t => esc(VEIC_ROT_CAT[t] || t);
  if(r.tipo){
    return `<b>${nome(r.tipo)}</b> ${veicConferido}`
      + (p.categoria && p.categoria !== r.tipo ? veicIaDisse(VEIC_ROT_CAT[p.categoria] || p.categoria, p.categoria_conf) : '');
  }
  const motivo = (p.observacoes || {}).tipo;
  return `<b ${p.categoria === 'veiculo' && motivo ? `title="${esc(motivo)}"` : ''}>${nome(p.categoria)}</b> <span class="veic-conf">${veicPct(p.categoria_conf)}</span>`;
}
function veicMarcaModelo(p){
  const r = p.rotulo || {};
  const ia = p.marca ? `${p.marca} ${p.modelo}` : '';
  if(r.marca){
    const humano = `${r.marca} ${r.modelo}`;
    return `<b>${esc(humano)}</b> ${veicConferido}${ia && ia !== humano ? veicIaDisse(ia, p.marca_modelo_conf) : ''}`;
  }
  if(ia) return `<b>${esc(ia)}</b> <span class="veic-conf">${veicPct(p.marca_modelo_conf)}</span>`;
  return veicNaoId((p.observacoes || {}).marca_modelo);
}
function veicCor(p){
  const r = p.rotulo || {};
  const bola = c => `<span class="veic-cor"><i data-cor="${esc(c)}"></i>${esc(c)}</span>`;
  if(r.cor){
    return `${bola(r.cor)} ${p.cor === r.cor ? `<span class="veic-conf">${veicPct(p.cor_conf)}</span> ` : ''}${veicConferido}`
      + (p.cor && p.cor !== r.cor ? veicIaDisse(p.cor, p.cor_conf) : '');
  }
  if(p.cor) return `${bola(p.cor)} <span class="veic-conf">${veicPct(p.cor_conf)}</span>`;
  return veicNaoId((p.observacoes || {}).cor);
}
function veicPlaca(p){
  if(p.placa) return `<span class="veic-placa">${esc(p.placa)}</span> <span class="veic-conf">${veicPct(p.placa_conf)}</span>`;
  return veicNaoId((p.observacoes || {}).placa);
}

/* Lista "em cena agora", vinda do evento ao vivo. */
function veicAoVivo(d){
  if(!d || d.camera_codigo !== VEIC.cam) return;
  VEIC.emCena = d.caixas || [];
  const box = $('#veicCena');
  if(!box) return;
  box.innerHTML = VEIC.emCena.length ? VEIC.emCena.map(c => `
    <div class="veic-item">
      <span class="veic-id">#${c.track_id}</span>
      <div><b>${esc(VEIC_ROT_CAT[c.classe] || c.classe)}</b> <span class="veic-conf">${veicPct(c.confianca)}</span>
        <div class="veic-sub">${[c.cor, c.marca ? `${c.marca} ${c.modelo || ''}` : '', c.placa]
          .filter(Boolean).map(esc).join(' · ') || 'identificação ao fim da passagem'}</div></div>
    </div>`).join('') : vazio('car', 'Nenhum veículo em cena.', '');
}

function veicModelosHTML(m, rotulados){
  const linha = (nome, s) => {
    if(!s.treinado) return `<dt>${nome}</dt><dd>aguardando rótulos — nada é afirmado ainda</dd>`;
    const r = s.relatorio || {};
    const prec = r.precisao_validacao != null ? ` · precisão medida ${Math.round(r.precisao_validacao * 100)}%` : '';
    return `<dt>${nome}</dt><dd>${s.liberadas.length ? esc(s.liberadas.join(', ')) : 'nenhuma classe liberada'}${prec}</dd>`;
  };
  const tipo = m.tipo ? linha('Tipo', {...m.tipo, liberadas: (m.tipo.liberadas || []).map(t => VEIC_ROT_CAT[t] || t)}) : '';
  return `<dl class="kv">${tipo}${linha('Cor', m.cor)}${linha('Marca/modelo', m.marca_modelo)}
    <dt>Rotulados</dt><dd>${fmtNum(rotulados)} veículo(s)</dd></dl>
    <div class="note" style="margin-top:12px">${ico('info',15)}<div>Tipo, cor e marca/modelo só aparecem quando a
    classe passou de 95% de precisão na validação. Rotule veículos na ficha (clique numa linha do histórico)
    e rode <code>py -3.13 -m scripts.veiculos_treinar</code>.</div></div>`;
}

async function renderVeiculos(alvo){
  const [est, cat] = await Promise.all([API.veicEstado(), VEIC.catalogo ? VEIC.catalogo : API.veicCatalogo()]);
  VEIC.catalogo = cat;
  const cams = est.cameras;
  if(!VEIC.cam || !cams.some(c => c.codigo === VEIC.cam))
    VEIC.cam = (cams.find(c => c.inteligencia_veicular) || cams[0] || {}).codigo || null;
  const camInfo = cams.find(c => c.codigo === VEIC.cam);
  const camObj = (APP.cameras || []).find(c => c.codigo === VEIC.cam);
  const f = VEIC.filtro;
  const vivo = camInfo && camInfo.ao_vivo;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Inteligência veicular</h2><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="veicCam">${cams.map(c => `<option value="${esc(c.codigo)}" ${c.codigo === VEIC.cam ? 'selected' : ''}>
        ${esc(c.codigo)} · ${esc(c.nome)}${c.inteligencia_veicular ? '' : ' · desligada'}</option>`).join('')}</select>
    </div></div>
  ${!cams.length ? `<div class="note" style="margin-bottom:14px">${ico('info',15)}<div>Nenhuma câmera marcada como
     <b>Alcança via, pátio ou garagem</b>. Marque em Câmeras › Configurar.</div></div>` : ''}
  ${camInfo && !camInfo.inteligencia_veicular ? `<div class="note" style="margin-bottom:14px">${ico('info',15)}<div>A
     inteligência veicular está <b>desligada</b> nesta câmera: os veículos seguem no registro geral, sem rastreio,
     cor e marca/modelo. Ligue em Câmeras › Configurar › Inteligência veicular.</div></div>` : ''}
  <div class="veic-grade">
    <div class="panel"><div class="panel-hd"><h3>${camObj ? `Câmera ao vivo — ${esc(camObj.nome)}` : 'Câmera ao vivo'}</h3>
        <span class="sub">${vivo ? `${esc(vivo.motor || est.servico.motor)} · ${vivo.ms_por_quadro} ms/quadro` : esc(est.servico.motor)}</span></div>
      <div class="panel-bd"><div class="veic-palco"><canvas id="veicFeed"></canvas></div></div></div>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="panel"><div class="panel-hd"><h3>Em cena agora</h3><span class="sub">${fmtNum(est.passagens_hoje)} passagem(ns) hoje</span></div>
        <div class="panel-bd" id="veicCena">${vazio('car', 'Nenhum veículo em cena.', '')}</div></div>
      <div class="panel"><div class="panel-hd"><h3>Modelos de identificação</h3></div>
        <div class="panel-bd">${veicModelosHTML(est.modelos, est.rotulados)}</div></div>
    </div>
  </div>
  <div class="panel" style="margin-top:14px"><div class="panel-hd"><h3>Histórico de passagens</h3><span class="sp"></span>
    <div class="toolbar">
      <select class="inp" id="veicFCat"><option value="">Todas as categorias</option>
        ${Object.entries(VEIC_ROT_CAT).map(([k, v]) => `<option value="${k}" ${f.categoria === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
      <select class="inp" id="veicFCor"><option value="">Todas as cores</option>
        ${cat.cores.map(c => `<option ${f.cor === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>
      <input class="inp" id="veicFPlaca" placeholder="Placa" value="${esc(f.placa)}" style="width:110px">
      <div class="periodo">
        <label for="veicFDe">Período</label>
        <input class="inp" id="veicFDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || diaISO(new Date()))}">
        <span class="sep">até</span>
        <input class="inp" id="veicFAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${diaISO(new Date())}">
      </div>
      <label class="veic-chk"><input type="checkbox" id="veicFRot" ${f.rotulagem ? 'checked' : ''}> só sem rótulo</label>
    </div></div>
    <div class="panel-bd tight" id="veicHist">${carregando()}</div></div>`;

  if(camObj){
    FEED.register($('#veicFeed'), camObj, {zonas: false, boxes: true, hd: true, conter: true});
    FEED.start();
  }
  VEIC.emCena = [];
  $('#veicCam').onchange = e => { VEIC.cam = e.target.value; VEIC.pagina = 1; irPara('vehicles'); };
  const aplicar = () => {
    VEIC.filtro = {categoria: $('#veicFCat').value, cor: $('#veicFCor').value,
                   placa: $('#veicFPlaca').value.trim(), rotulagem: $('#veicFRot').checked,
                   de: $('#veicFDe').value, ate: $('#veicFAte').value};
    $('#veicFDe').max = VEIC.filtro.ate || diaISO(new Date());
    $('#veicFAte').min = VEIC.filtro.de;
    VEIC.pagina = 1; veicCarregarHistorico();
  };
  ['veicFCat', 'veicFCor', 'veicFRot', 'veicFDe', 'veicFAte'].forEach(id => $('#' + id).onchange = aplicar);
  $('#veicFPlaca').oninput = debounce(aplicar, 400);
  await veicCarregarHistorico();
}

async function veicCarregarHistorico(){
  const box = $('#veicHist');
  if(!box) return;
  const f = VEIC.filtro;
  try{
    const r = await API.veicPassagens({camera: VEIC.cam, categoria: f.categoria, cor: f.cor,
                                      placa: f.placa, rotulagem: f.rotulagem || undefined,
                                      desde: limiteDia(f.de), ate: limiteDia(f.ate, true),
                                      pagina: VEIC.pagina, por_pagina: 20});
    box.innerHTML = `<div class="tbl-wrap"><table class="tbl">
      <thead><tr><th style="width:84px">Captura</th><th>Veículo</th><th>Cor</th><th>Marca / modelo</th>
        <th>Placa</th><th>Câmera</th><th>Data / hora</th></tr></thead>
      <tbody>${(r.itens || []).map(p => `
        <tr class="clickable" data-veic="${esc(p.id)}">
          <td><img class="veic-mini" loading="lazy" src="${API.veicImagem(p.id, 'recorte')}" alt=""></td>
          <td>${veicTipo(p)}
            <br><span class="td-id">#${p.trilha} · ${p.quadros} quadros${p.rotulo ? ' · rotulado' : ''}</span></td>
          <td>${veicCor(p)}</td>
          <td>${veicMarcaModelo(p)}</td>
          <td>${veicPlaca(p)}</td>
          <td><b>${esc(p.camera ? p.camera.codigo : '')}</b><br><span class="td-ts">${esc(p.camera ? p.camera.nome : '')}</span></td>
          <td class="td-ts">${fmtDT(p.inicio)}<br><span style="color:var(--txt-4)">${relTime(p.inicio)}</span></td>
        </tr>`).join('') || `<tr><td colspan="7">${vazio('car', 'Nenhuma passagem registrada.',
          'Com a inteligência veicular ligada, cada veículo que passa vira uma linha aqui.')}</td></tr>`}
      </tbody></table></div>${pagerHTML(r.total, r.pagina, r.por_pagina, 'veic')}`;
    $$('tr[data-veic]', box).forEach(t => t.onclick = () => veicAbrir(t.dataset.veic));
    $$('[data-pg]', box).forEach(b => b.onclick = () => {
      VEIC.pagina = +b.dataset.pg.split(':')[1]; veicCarregarHistorico();
    });
  }catch(e){ box.innerHTML = erroBloco(e); }
}

async function veicAbrir(id){
  let p;
  try{ p = await API.veicPassagem(id); }catch(e){ erroToast(e); return; }
  // Tipos, cores e modelos vêm sempre do servidor, relidos a cada ficha: a
  // tela nunca oferece uma opção que o servidor recusaria. Foi assim que
  // "Kombi" deu "Tipo desconhecido" — a lista da tela era mais nova que a
  // do servidor em execução.
  try{ VEIC.catalogo = await API.veicCatalogo(); }catch(e){ /* fica a última lida */ }
  const cat = VEIC.catalogo || {cores: [], modelos: [], tipos: []};
  const tiposRotulo = (cat.tipos || []).length
    ? cat.tipos.filter(t => t.valor !== 'veiculo').map(t => ({valor: t.valor, nome: VEIC_ROT_CAT[t.valor] || t.nome}))
    : VEIC_TIPOS_ROTULO.map(t => ({valor: t, nome: VEIC_ROT_CAT[t]}));
  const rot = p.rotulo || {tipo: '', cor: '', marca: '', modelo: ''};
  const obs = p.observacoes || {};
  const mmAtual = rot.marca ? `${rot.marca}|${rot.modelo}` : '';
  const noCatalogo = cat.modelos.some(m => `${m.marca}|${m.modelo}` === mmAtual);
  const box = abrirModal(`
    <div class="modal-hd"><h3>${esc(VEIC_ROT_CAT[rot.tipo || p.categoria] || rot.tipo || p.categoria)} #${p.trilha}
      <span class="sub">${esc(p.camera ? p.camera.nome : '')} · ${fmtDT(p.inicio)}</span></h3>
      <button class="x" data-close aria-label="Fechar">${ico('x',16)}</button></div>
    <div class="modal-bd">
      <div class="veic-ficha">
        <div class="veic-imgs">
          ${p.tem_quadro ? `<figure><div class="veic-quadro"><img src="${API.veicImagem(p.id, 'quadro')}" alt="Quadro inteiro">
            ${p.bbox ? `<span class="veic-alvo" style="left:${p.bbox[0]*100}%;top:${p.bbox[1]*100}%;width:${p.bbox[2]*100}%;height:${p.bbox[3]*100}%"></span>` : ''}</div>
            <figcaption>Quadro inteiro — o veículo desta passagem está marcado</figcaption></figure>` : ''}
          <figure><img src="${API.veicImagem(p.id, 'recorte')}" alt="Recorte do veículo"><figcaption>Recorte do veículo</figcaption></figure>
          ${p.tem_placa ? `<figure><img src="${API.veicImagem(p.id, 'placa')}" alt="Placa"><figcaption>Placa lida</figcaption></figure>` : ''}
        </div>
        <div>
          <dl class="kv">
            <dt>Tipo</dt><dd>${veicTipo(p)}${!rot.tipo && obs.tipo ? `<div class="veic-sub">${esc(obs.tipo)}</div>` : ''}</dd>
            <dt>Cor</dt><dd>${veicCor(p)}${!p.cor && !rot.cor && obs.cor ? `<div class="veic-sub">${esc(obs.cor)}</div>` : ''}</dd>
            <dt>Marca / modelo</dt><dd>${veicMarcaModelo(p)}${!p.marca && !rot.marca && obs.marca_modelo ? `<div class="veic-sub">${esc(obs.marca_modelo)}</div>` : ''}</dd>
            <dt>Placa</dt><dd>${veicPlaca(p)}${!p.placa && obs.placa ? `<div class="veic-sub">${esc(obs.placa)}</div>` : ''}</dd>
            <dt>Rastreio</dt><dd>#${p.trilha} · ${p.quadros} quadros · ${fmtDT(p.inicio)} → ${fmtDT(p.fim)}</dd>
            ${p.deteccao ? `<dt>Registro</dt><dd><a href="#" id="veicDet">${esc(p.deteccao)}</a></dd>` : ''}
          </dl>
          <div class="veic-rotulo">
            <div class="eyebrow">O que este veículo é? (treino e medição)</div>
            <p class="veic-sub">Não muda este registro: ensina e mede os modelos. Deixe em branco o que não souber —
              rótulo errado ensina errado.</p>
            <label>Tipo<select class="inp" id="veicRTipo"><option value="">não sei</option>
              ${tiposRotulo.map(t => `<option value="${esc(t.valor)}" ${rot.tipo === t.valor ? 'selected' : ''}>${esc(t.nome)}</option>`).join('')}</select></label>
            <p class="veic-sub">Caminhão pequeno: VUC, 3/4, baú leve (Accelo, Delivery, HR/Bongo baú).
              Caminhão grande: toco, truck, carreta, bitrem. Van: Fiorino, Ducato, Sprinter, Master, Expert. Kombi tem tipo próprio.</p>
            <label>Cor<select class="inp" id="veicRCor"><option value="">não sei</option>
              ${cat.cores.map(c => `<option ${rot.cor === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></label>
            <label>Marca / modelo<select class="inp" id="veicRMM"><option value="">não sei</option>
              ${cat.modelos.filter(m => (m.categoria === 'moto') === (p.categoria === 'motocicleta'))
                .map(m => { const v = `${m.marca}|${m.modelo}`;
                  return `<option value="${esc(v)}" ${mmAtual === v ? 'selected' : ''}>${esc(m.marca)} ${esc(m.modelo)}</option>`; }).join('')}
              <option value="__outro" ${mmAtual && !noCatalogo ? 'selected' : ''}>outro modelo (digitar)…</option></select></label>
            <div class="veic-outro" id="veicROutro" ${mmAtual && !noCatalogo ? '' : 'hidden'}>
              <input class="inp" id="veicRMarca" placeholder="Marca" value="${esc(mmAtual && !noCatalogo ? rot.marca : '')}">
              <input class="inp" id="veicRModelo" placeholder="Modelo" value="${esc(mmAtual && !noCatalogo ? rot.modelo : '')}">
            </div>
            ${(obs.sugestoes_modelo || []).length && p.categoria !== 'motocicleta' ? `
            <div class="veic-sug"><span class="veic-sub">Sugestões da IA (não conferidas) — clique para preencher:</span>
              ${obs.sugestoes_modelo.map(s => `<button type="button" class="veic-sug-b" data-mm="${esc(s.marca + '|' + s.modelo)}">
                ${esc(s.marca)} ${esc(s.modelo)} <small>${veicPct(s.conf)}</small></button>`).join('')}</div>` : ''}
            <div class="veic-botoes">
              <button class="btn btn-primary btn-sm" id="veicRSalvar">${ico('check',14)} Salvar rótulo</button>
              <button class="btn btn-soft btn-sm" id="veicRProximo" title="Salva e abre a próxima passagem ainda sem rótulo">Salvar e próximo ${ico('chev',13)}</button>
              ${p.rotulo ? `<button class="btn btn-ghost btn-sm" id="veicRExcluir" title="Apaga só o rótulo; a passagem volta para a fila de rotulagem">${ico('x',13)} Excluir rótulo</button>` : ''}
            </div>
            <div class="veic-excluir">
              <button class="btn btn-danger btn-sm" id="veicExcluir" title="Remove esta passagem do histórico e do treino">${ico('trash',13)} Excluir passagem</button>
              <span class="veic-sub">Para registro errado — recorte de outra coisa, veículo que não existe.</span>
            </div>
          </div>
        </div>
      </div>
    </div>`, true);
  const det = $('#veicDet', box);
  if(det) det.onclick = e => { e.preventDefault(); fecharModal(); abrirDeteccao(p.deteccao); };
  const sel = $('#veicRMM', box);
  sel.onchange = () => {
    $('#veicROutro', box).hidden = sel.value !== '__outro';
    // Escolheu a Kombi como modelo e ainda não disse o tipo: o tipo é Kombi.
    const tipo = $('#veicRTipo', box);
    if(sel.value === 'Volkswagen|Kombi' && !tipo.value) tipo.value = 'kombi';
  };
  $$('.veic-sug-b', box).forEach(b => b.onclick = () => {
    const v = b.dataset.mm;
    if([...sel.options].some(o => o.value === v)){
      sel.value = v;
    }else{
      sel.value = '__outro';
      [$('#veicRMarca', box).value, $('#veicRModelo', box).value] = v.split('|');
    }
    sel.onchange();
    $$('.veic-sug-b', box).forEach(x => x.classList.toggle('on', x === b));
  });
  // Confere aqui o que o servidor recusaria, para não chegar a ele um
  // pedido que só voltaria como erro.
  const salvar = async () => {
    let marca = '', modelo = '';
    if(sel.value === '__outro'){
      marca = $('#veicRMarca', box).value.trim(); modelo = $('#veicRModelo', box).value.trim();
      if(!marca || !modelo){
        toast('Falta a marca ou o modelo', 'Preencha os dois campos, ou escolha "não sei".', 'warn');
        throw null;
      }
    }else if(sel.value){
      [marca, modelo] = sel.value.split('|');
    }
    const tipo = $('#veicRTipo', box).value;
    if(tipo && !tiposRotulo.some(t => t.valor === tipo)) throw null;
    await API.veicRotular(p.id, {tipo, cor: $('#veicRCor', box).value, marca, modelo});
  };
  const falhou = e => { if(e) erroToast(e); };
  $('#veicRSalvar', box).onclick = async () => {
    try{
      await salvar();
      toast('Rótulo salvo', 'Entra no próximo treino dos modelos.', 'ok');
      fecharModal();
      veicCarregarHistorico();
    }catch(e){ falhou(e); }
  };
  $('#veicExcluir', box).onclick = async () => {
    if(!confirm('Excluir esta passagem? Ela sai do histórico e do treino, com o rótulo. Não dá para desfazer.')) return;
    try{
      await API.veicExcluirPassagem(p.id);
      toast('Passagem excluída', 'Saiu do histórico e do treino.', 'ok');
      fecharModal();
      veicCarregarHistorico();
    }catch(e){ falhou(e); }
  };
  const excluir = $('#veicRExcluir', box);
  if(excluir) excluir.onclick = async () => {
    if(!confirm('Excluir o rótulo desta passagem? Ela volta para a fila de rotulagem.')) return;
    try{
      await API.veicExcluirRotulo(p.id);
      toast('Rótulo excluído', 'A passagem voltou para a fila de rotulagem.', 'ok');
      fecharModal();
      veicCarregarHistorico();
    }catch(e){ falhou(e); }
  };
  $('#veicRProximo', box).onclick = async () => {
    try{
      await salvar();
      const r = await API.veicPassagens({camera: VEIC.cam, rotulagem: true, por_pagina: 1});
      veicCarregarHistorico();
      if(r.itens && r.itens.length) veicAbrir(r.itens[0].id);
      else { fecharModal(); toast('Tudo rotulado', 'Não há mais passagens sem rótulo nesta câmera.', 'ok'); }
    }catch(e){ falhou(e); }
  };
}
