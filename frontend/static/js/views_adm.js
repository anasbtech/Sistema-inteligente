/* ==========================================================
   SEGURANÇA — Alertas, usuários, auditoria e configurações
   ========================================================== */
'use strict';

/* ==========================================================
   ALERTAS
   ========================================================== */
/* De onde sai a imagem deste alerta.

   A captura que o gerou, quando houve uma. Quando não houve — alerta de sala
   reportando que ela ficou vazia, por exemplo — o próprio alerta guarda o
   quadro daquele instante. Sem esta escolha a coluna CAPTURA ficava em branco
   justamente nos alertas que mais precisam dela. */
function imagemDoAlerta(a){
  if(a.deteccao_codigo) return {codigo: a.deteccao_codigo, fonte: 'deteccoes'};
  if(a.tem_imagem)      return {codigo: a.codigo, fonte: 'alertas'};
  return null;
}

const ROTULO_TIPO_ALERTA = {FACIAL: 'Facial', OBJETO: 'Objeto', ACAO: 'Ação'};
const COR_TIPO_ALERTA = {FACIAL: 'c-blue', OBJETO: 'c-green', ACAO: 'c-violet'};

async function renderAlertas(alvo){
  const resumo = await API.resumoAlertas();
  const f = FILTRO.alertas, hoje = diaISO(new Date());
  const porPagina = +f.por_pagina || POR_PAGINA;
  const podeAgir = API.pode('edit');
  // Seleção da triagem em lote: códigos marcados nesta página.
  const sel = new Set();

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Central de alertas</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="segm" id="alSeg">
        <button data-s="" class="${!f.status?'on':''}">Todos</button>
        <button data-s="ABERTO" class="${f.status==='ABERTO'?'on':''}">Abertos (${resumo.abertos||0})</button>
        <button data-s="EM_ANDAMENTO" class="${f.status==='EM_ANDAMENTO'?'on':''}">Em curso (${resumo.em_andamento||0})</button>
        <button data-s="RESOLVIDO" class="${f.status==='RESOLVIDO'?'on':''}">Resolvidos (${resumo.resolvidos||0})</button>
      </div>
      <select class="inp" id="alSev"><option value="">Qualquer severidade</option>
        ${[4,3,2].map(s => `<option value="${s}" ${f.severidade==String(s)?'selected':''}>${SEVERIDADE[s]}</option>`).join('')}</select>
      <select class="inp" id="alCam"><option value="">Todas as câmeras</option>${opcoesCameras(null, f.cam)}</select>
      <div class="periodo">
        <label for="alDe">Período</label>
        <input class="inp" id="alDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || hoje)}">
        <span class="sep">até</span>
        <input class="inp" id="alAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${hoje}">
        <button class="btn btn-ghost btn-sm" id="alLimpar" ${f.de||f.ate?'':'hidden'}>Limpar</button>
      </div>
    </div></div>
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-red"><div class="kpi-top"><span class="kpi-lbl">Abertos</span><span class="kpi-ico">${ico('bell',15)}</span></div><div class="kpi-val">${resumo.abertos||0}</div><div class="kpi-delta dn">aguardando triagem</div></div>
    <div class="kpi k-amber"><div class="kpi-top"><span class="kpi-lbl">Em andamento</span><span class="kpi-ico">${ico('clock',15)}</span></div><div class="kpi-val">${resumo.em_andamento||0}</div><div class="kpi-delta nt">com responsável atribuído</div></div>
    <div class="kpi k-green"><div class="kpi-top"><span class="kpi-lbl">Resolvidos</span><span class="kpi-ico">${ico('check',15)}</span></div><div class="kpi-val">${resumo.resolvidos||0}</div><div class="kpi-delta up">com desfecho registrado</div></div>
    <div class="kpi"><div class="kpi-top"><span class="kpi-lbl">Tempo médio de resposta</span><span class="kpi-ico">${ico('gauge',15)}</span></div>
      <div class="kpi-val" style="font-size:22px">${resumo.tempo_medio_resposta_s ? duracao(resumo.tempo_medio_resposta_s) : '—'}</div>
      <div class="kpi-delta nt">${resumo.criticos_pendentes||0} crítico(s) pendente(s)</div></div>
  </div>
  ${podeAgir ? `
  <div class="lote-barra" id="alLote" hidden>
    <div class="lote-topo">
      <b id="alLoteN">0 selecionados</b>
      <span class="lote-dica">Encerrar todos com o mesmo desfecho — cada um ganha a sua linha na trilha de auditoria.</span>
      <button class="btn btn-ghost btn-sm" id="alLoteLimpar">Limpar seleção</button>
    </div>
    <div class="lote-corpo">
      <div class="motivos" role="radiogroup">
        ${ALERTA.MOTIVOS.map((m, i) => `<button type="button" class="motivo" role="radio"
             aria-checked="false" data-lote-motivo="${i}">${esc(m.rotulo)}</button>`).join('')}
      </div>
      <input class="inp" id="alLoteObs" placeholder="Observação (obrigatória em “Outro”)">
      <button class="btn btn-primary btn-sm" id="alLoteOk" disabled>${ico('check',13)} Encerrar selecionados</button>
    </div>
  </div>` : ''}
  <div class="panel"><div class="panel-bd tight"><div id="alCorpo">${carregando()}</div></div></div>`;

  /* ---------- Seleção e encerramento em lote ---------- */
  let motivoLote = null;
  const atualizarLote = () => {
    const barra = $('#alLote');
    if(!barra) return;
    barra.hidden = sel.size === 0;
    $('#alLoteN').textContent = `${sel.size} ${sel.size === 1 ? 'selecionado' : 'selecionados'}`;
    const m = ALERTA.MOTIVOS[motivoLote];
    $('#alLoteOk').disabled = !m || (m.exigeTexto && $('#alLoteObs').value.trim().length < 8);
    const todos = $('#alTodos');
    if(todos){
      const marcaveis = $$('[data-sel]', $('#alCorpo'));
      todos.checked = marcaveis.length > 0 && marcaveis.every(c => c.checked);
      todos.indeterminate = !todos.checked && marcaveis.some(c => c.checked);
    }
  };
  if(podeAgir){
    $$('[data-lote-motivo]').forEach(b => b.onclick = () => {
      motivoLote = +b.dataset.loteMotivo;
      $$('[data-lote-motivo]').forEach(x => x.setAttribute('aria-checked', String(x === b)));
      atualizarLote();
    });
    $('#alLoteObs').oninput = atualizarLote;
    $('#alLoteLimpar').onclick = () => {
      sel.clear();
      $$('[data-sel]', $('#alCorpo')).forEach(c => { c.checked = false; });
      atualizarLote();
    };
    $('#alLoteOk').onclick = async () => {
      const m = ALERTA.MOTIVOS[motivoLote];
      const nota = $('#alLoteObs').value.trim();
      const resolucao = m.exigeTexto ? nota : `${m.desfecho}${nota ? ` Observação: ${nota}` : ''}`;
      // Guardado antes da pergunta: `confirmar` esvazia a caixa do modal ao
      // fechar, e o botão pode não existir mais quando ela responder.
      const botaoOk = $('#alLoteOk');
      const ok = await confirmar('Encerrar alertas em lote',
        `<b>${sel.size}</b> alerta(s) serão encerrados como <b>${esc(m.rotulo.toLowerCase())}</b>.<br><br>
         Cada um ganha a sua linha na trilha de auditoria, com o seu nome e este desfecho.`,
        {rotulo: `Encerrar ${sel.size}`});
      if(!ok) return;
      if(botaoOk) botaoOk.disabled = true;
      try{
        const r = await API.encerrarAlertasLote([...sel], resolucao);
        toast('Alertas encerrados',
          `${r.encerrados} encerrado(s)${r.ja_encerrados.length ? ` · ${r.ja_encerrados.length} já estavam encerrados` : ''}.`, 'ok');
        atualizarContadoresDebounced();
        irPara('alerts');
      }catch(err){ atualizarLote(); erroToast(err); }
    };
  }

  const carregar = async () => {
    const corpo = $('#alCorpo');
    corpo.innerHTML = carregando();
    sel.clear();
    atualizarLote();
    try{
      const r = await API.alertas({
        pagina: PAG.alertas, por_pagina: porPagina,
        status: FILTRO.alertas.status || undefined,
        severidade: FILTRO.alertas.severidade || undefined,
        camera: FILTRO.alertas.cam || undefined,
        desde: limiteDia(FILTRO.alertas.de),
        ate: limiteDia(FILTRO.alertas.ate, true)
      });
      const itens = r.itens || [];
      const algumMarcavel = podeAgir && itens.some(a => a.status !== 'RESOLVIDO');
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:8px"></th>
          ${podeAgir ? `<th style="width:34px">${algumMarcavel
            ? '<input type="checkbox" id="alTodos" class="sel-alerta" aria-label="Selecionar todos desta página">' : ''}</th>` : ''}
          <th style="width:64px">Captura</th>
          <th>Alerta</th><th>Tipo</th><th>Câmera</th>
          <th>Data / hora</th><th>Status</th><th style="width:96px"></th></tr></thead>
        <tbody>${itens.map(a => {
          const st = STATUS_ALERTA[a.status] || {};
          const img = imagemDoAlerta(a);
          return `<tr class="clickable" data-a="${esc(a.codigo)}">
            <td style="padding:0"><div class="sev sev-${a.severidade}" style="height:100%;min-height:44px;width:3px"></div></td>
            ${podeAgir ? `<td>${a.status !== 'RESOLVIDO'
              ? `<input type="checkbox" class="sel-alerta" data-sel="${esc(a.codigo)}" aria-label="Selecionar ${esc(a.codigo)}">` : ''}</td>` : ''}
            <td>${img ? `<canvas data-snap="${snapAttr(img, img.fonte)}" width="54"></canvas>` : ''}</td>
            <td><b>${esc(a.titulo)}</b><br><span class="td-ts">${esc((a.detalhe||'').slice(0,54))}${(a.detalhe||'').length>54?'…':''}</span></td>
            <td><span class="chip ${COR_TIPO_ALERTA[a.tipo] || 'c-gray'}">${esc(ROTULO_TIPO_ALERTA[a.tipo] || a.tipo)}</span></td>
            <td><b>${esc(a.camera?a.camera.codigo:'')}</b><br><span class="td-ts">${esc((a.camera?a.camera.nome:'').slice(0,24))}</span></td>
            <td class="td-ts">${fmtDT(a.disparado_em)}<br><span style="color:var(--txt-4)">${relTime(a.disparado_em)}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span>${a.atribuido_nome ? `<br><span class="td-ts">${esc(a.atribuido_nome)}</span>` : ''}</td>
            <td style="text-align:right">${a.status==='ABERTO' && podeAgir
              ? `<button class="btn btn-soft btn-sm" data-assume="${esc(a.codigo)}">Assumir</button>` : ''}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="9">${vazio('check','Nenhum alerta neste filtro.')}</td></tr>`}
        </tbody></table></div>
        <div class="tbl-rodape">
          <label class="por-pagina">Por página
            <select class="inp" id="alPorPagina">${[POR_PAGINA, 50, 100, 200].map(n =>
              `<option value="${n}" ${n === porPagina ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
          ${pagerHTML(r.total, r.pagina, r.por_pagina, 'alertas')}
        </div>`;

      pintarSnaps(corpo);
      $$('tr[data-a]', corpo).forEach(t => t.onclick = e => {
        if(e.target.closest('.sel-alerta')) return;      // marcar não abre a ficha
        abrirAlerta(t.dataset.a);
      });
      $$('[data-sel]', corpo).forEach(c => c.onchange = () => {
        c.checked ? sel.add(c.dataset.sel) : sel.delete(c.dataset.sel);
        atualizarLote();
      });
      const todos = $('#alTodos', corpo);
      if(todos) todos.onchange = () => {
        $$('[data-sel]', corpo).forEach(c => {
          c.checked = todos.checked;
          todos.checked ? sel.add(c.dataset.sel) : sel.delete(c.dataset.sel);
        });
        atualizarLote();
      };
      $$('[data-assume]', corpo).forEach(b => b.onclick = async e => {
        e.stopPropagation();
        try{
          await API.assumirAlerta(b.dataset.assume);
          toast('Alerta assumido', `${b.dataset.assume} atribuído a ${API.usuario.nome}.`, 'ok');
          irPara('alerts');
        }catch(err){ erroToast(err); }
      });
      $('#alPorPagina', corpo).onchange = e => {
        FILTRO.alertas.por_pagina = +e.target.value; PAG.alertas = 1; irPara('alerts');
      };
      ligarPager(corpo, 'alertas', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => { PAG.alertas = 1; FILTRO.alertas = {...FILTRO.alertas, de: $('#alDe').value, ate: $('#alAte').value}; $('#alDe').max = FILTRO.alertas.ate || hoje; $('#alAte').min = FILTRO.alertas.de; $('#alLimpar').hidden = !(FILTRO.alertas.de || FILTRO.alertas.ate); carregar(); };
  $$('#alSeg button').forEach(b => b.onclick = () => {
    FILTRO.alertas.status = b.dataset.s; PAG.alertas = 1; irPara('alerts');
  });
  $('#alSev').onchange = e => { FILTRO.alertas.severidade = e.target.value; PAG.alertas = 1; carregar(); };
  $('#alCam').onchange = e => { FILTRO.alertas.cam = e.target.value; PAG.alertas = 1; carregar(); };
  ['alDe','alAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#alLimpar').onclick = () => { $('#alDe').value = ''; $('#alAte').value = ''; aplicar(); };
  await carregar();
}

async function abrirAlerta(codigo){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let a;
  try{ a = await API.alerta(codigo); }
  catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const st = STATUS_ALERTA[a.status] || {};
  const p = a.pessoa;
  const resolvido = a.status === 'RESOLVIDO';
  const podeFechar = !resolvido && API.pode('edit');
  const critico = a.severidade >= 4;
  const img = imagemDoAlerta(a);
  const cam = a.camera || {};
  const nivel = critico ? 'Alerta crítico'
              : `Alerta · gravidade ${(SEVERIDADE[a.severidade] || '').toLowerCase()}`;
  // Quanto tempo ficou (ou está) aberto: é o número que a supervisão cobra.
  const fimContagem = a.fechado_em ? new Date(a.fechado_em) : new Date();
  const aberto_s = Math.max(0, (fimContagem - new Date(a.disparado_em)) / 1000);

  /* Linha do tempo: o que já aconteceu com este alerta, na ordem. */
  const etapas = [
    {feito: true, titulo: 'Disparado', det: fmtDT(a.disparado_em)},
    {feito: !!a.atribuido_nome || a.status !== 'ABERTO', titulo: 'Assumido',
     det: a.atribuido_nome ? `por ${a.atribuido_nome}` : 'aguardando responsável'},
    {feito: resolvido, titulo: 'Encerrado',
     det: a.fechado_em ? fmtDT(a.fechado_em) : 'pendente'}
  ];

  box.className = 'modal wide ficha-alerta';
  box.innerHTML = `
    <header class="ficha-topo ${critico ? '' : 'sev-media'}">
      <span class="alerta-sev"><span class="pulso"></span>${esc(nivel)}</span>
      <span class="ficha-cod">${esc(a.codigo)}</span>
      <span class="chip ${st.cls}">${esc(st.lbl)}</span>
      <button class="x" data-close aria-label="Fechar">${ico('x',17)}</button>
    </header>

    <div class="modal-bd ficha-corpo">
      <div class="ficha-cabeca">
        <h2>${esc(a.titulo)}</h2>
        <div class="alerta-local">
          ${cam.nome ? `<span class="etiqueta">${ico('cam', 13)}${esc(cam.nome)}</span>` : ''}
          ${cam.codigo ? `<span class="etiqueta mono">${esc(cam.codigo)}</span>` : ''}
          ${cam.setor && cam.setor !== cam.nome ? `<span class="etiqueta">${ico('pin', 13)}${esc(cam.setor)}</span>` : ''}
          <span class="etiqueta">${esc(ROTULO_TIPO_ALERTA[a.tipo] || a.tipo)}</span>
        </div>
      </div>

      <div class="ficha-colunas">
        ${img ? `<figure class="ficha-foto">
          <div class="moldura" id="alMold"><canvas id="alSnap"></canvas>
            <span class="rotulo-foto">${a.deteccao_codigo ? 'Captura que gerou o alerta' : 'A sala no momento do alerta'}</span></div>
          <figcaption>${esc(img.codigo)}${cam.nome ? ` · ${esc(cam.nome)}` : ''}</figcaption>
        </figure>` : ''}

        <div class="ficha-info">
          ${a.detalhe ? `<p class="ficha-texto">${esc(a.detalhe)}</p>` : ''}

          <dl class="alerta-dados">
            <div><dt>Disparado em</dt><dd class="mono">${fmtDT(a.disparado_em)}</dd></div>
            <div><dt>${resolvido ? 'Tempo até o encerramento' : 'Aberto há'}</dt><dd class="mono">${duracao(aberto_s)}</dd></div>
            <div><dt>Gravidade</dt><dd><span class="chip ${CLS_SEV[a.severidade]}">${esc(SEVERIDADE[a.severidade])}</span></dd></div>
            <div><dt>Responsável</dt><dd>${a.atribuido_nome ? esc(a.atribuido_nome) : '<span class="vazio-txt">ninguém assumiu</span>'}</dd></div>
          </dl>

          ${p ? `<div class="ficha-pessoa">
            <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="44" height="44"></canvas>
            <div><span class="eyebrow">Pessoa identificada</span><b>${esc(p.nome)}</b>
              <span class="mono">${esc(p.id_biometrico)}</span></div>
            <span class="chip ${(STATUS_PESSOA[p.status]||{}).cls}">${esc((STATUS_PESSOA[p.status]||{}).lbl)}</span>
          </div>` : ''}

          <ol class="ficha-tempo" aria-label="Andamento do alerta">
            ${etapas.map(e => `<li class="${e.feito ? 'feito' : ''}">
              <span class="marco"></span><b>${esc(e.titulo)}</b><span>${esc(e.det)}</span></li>`).join('')}
          </ol>
        </div>
      </div>

      ${resolvido ? `<div class="ficha-desfecho">
        <span class="eyebrow">${ico('check', 13)} Desfecho registrado</span>
        <p>${esc(a.resolucao || '—')}</p>
      </div>` : ''}

      ${podeFechar ? `<div class="ficha-encerrar">
        <label for="alRes" class="titulo-enc">Registrar o desfecho</label>
        <div class="motivos" role="group" aria-label="Desfechos comuns">
          ${ALERTA.MOTIVOS.map((m, i) => `<button type="button" class="motivo" data-al-motivo="${i}">${esc(m.rotulo)}</button>`).join('')}
        </div>
        <textarea class="inp" id="alRes" rows="3" placeholder="Descreva a providência adotada — despacho de guarnição, confirmação de falso positivo, encaminhamento…"></textarea>
        <span class="hint">Obrigatório para encerrar. Vai para a trilha de auditoria com o seu nome.</span>
      </div>` : ''}
    </div>

    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${API.pode('settings') ? `<button class="btn btn-danger" id="alDel">Excluir</button>` : ''}
      <span class="sp" style="flex:1"></span>
      ${a.camera && API.pode('live') ? `<button class="btn btn-soft" id="alCam">${ico('live',14)} Ver câmera</button>` : ''}
      ${podeFechar && a.status === 'ABERTO'
        ? `<button class="btn btn-soft" id="alAssume">${ico('users',14)} Assumir</button>` : ''}
      ${podeFechar ? `<button class="btn btn-primary" id="alClose">${ico('check',14)} Encerrar alerta</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);

  /* Assumir existia na lista e faltava aqui — quem abre o alerta para
     trabalhar nele é justamente quem deveria poder dizer que está cuidando.
     Sem isso, duas pessoas podem despachar guarnição para a mesma coisa, e
     o encerramento não registra quem assumiu a responsabilidade. */
  const bass = $('#alAssume', box);
  if(bass) bass.onclick = async () => {
    bass.disabled = true;
    try{
      await API.assumirAlerta(a.codigo);
      toast('Alerta assumido', `${a.codigo} atribuído a ${API.usuario.nome}.`, 'ok');
      fecharModal();
      abrirAlerta(a.codigo);
    }catch(err){ bass.disabled = false; erroToast(err); }
  };
  pintarFaces(box);

  /* A moldura tem tamanho fixo e o recorte se encaixa dentro dela, inteiro e
     sem esticar — em pé ou deitado. É o que dá à ficha a mesma forma em todo
     alerta, em vez de a imagem empurrar o texto. */
  const snap = $('#alSnap', box);
  if(snap && img){
    molduraVazia(snap, 300, 340);
    carregarRecorte(snap, img.codigo, 300, img.fonte, 340);
  }

  const bc = $('#alCam', box); if(bc) bc.onclick = () => { fecharModal(); abrirCamera(a.camera.codigo); };

  /* Atalho de desfecho: preenche o texto, que continua editável. "Outro"
     limpa o campo e põe o cursor nele. */
  const res = $('#alRes', box);
  $$('[data-al-motivo]', box).forEach(b => b.onclick = () => {
    const m = ALERTA.MOTIVOS[+b.dataset.alMotivo];
    $$('[data-al-motivo]', box).forEach(x => x.classList.toggle('on', x === b));
    res.value = m.exigeTexto ? '' : m.desfecho;
    res.focus();
  });

  const bd = $('#alDel', box);
  if(bd) bd.onclick = async () => {
    const motivo = await confirmar('Excluir alerta',
      `<b>${esc(a.codigo)}</b> — ${esc(a.titulo)}<br><br>
       Encerrar com desfecho preserva o histórico; excluir não se desfaz.
       A detecção que originou este alerta <b>permanece</b> nos registros —
       o que sai é a pendência de triagem.`,
      {rotulo:'Excluir alerta', perigo:true, exigeTexto:'Motivo da exclusão'});
    if(!motivo) return;
    try{
      await API.excluirAlerta(a.codigo, motivo);
      fecharModal();
      toast('Alerta excluído', `${a.codigo} — motivo registrado na trilha de auditoria.`, 'ok');
      irPara('alerts');
    }catch(e){ erroToast(e); }
  };

  const bClose = $('#alClose', box);
  if(bClose) bClose.onclick = async () => {
    const txt = res.value.trim();
    if(txt.length < 8){ toast('Desfecho obrigatório', 'Descreva a providência adotada antes de encerrar.', 'err'); res.focus(); return; }
    try{
      await API.encerrarAlerta(a.codigo, txt);
      fecharModal();
      toast('Alerta encerrado', `${a.codigo} — desfecho registrado na trilha de auditoria.`, 'ok');
      atualizarContadoresDebounced();
      irPara('alerts');
    }catch(e){ erroToast(e); }
  };
}

/* ==========================================================
   USUÁRIOS
   ========================================================== */
const PERMS_COLUNAS = ['live','cams','facial','objects','actions','biometrics','alerts','users','audit','settings','export','edit'];
// "objects" e "actions" apareciam crus, em inglês, no meio dos rótulos em
// português: faltavam aqui desde que as duas permissões foram separadas.
const PERMS_ROTULO = {live:'Ao vivo', cams:'Câmeras', facial:'Reconhecimento facial',
  objects:'Detecção de objetos', actions:'Análise de ações', biometrics:'Base biométrica',
  alerts:'Alertas', users:'Usuários', audit:'Auditoria', settings:'Configurações',
  export:'Exportar', edit:'Editar e tratar'};

/* Nome legível de cada ação da trilha. O código continua no filtro e na
   exportação — é ele que a perícia procura —, mas a tabela fala português. */
const ROTULO_AUDITORIA = {
  LOGIN:'Entrada no sistema', LOGIN_FALHA:'Tentativa de entrada recusada',
  LOGIN_BLOQUEADO:'Conta bloqueada por tentativas', LOGOUT:'Saída do sistema',
  SENHA_RESET:'Redefinição de senha', SENHA_RESET_SEM_CONTA:'Redefinição para e-mail sem conta',
  SENHA_RESET_FALHA_ENVIO:'Falha ao enviar e-mail de redefinição',
  USR_ADD:'Usuário criado', USR_EDIT:'Usuário alterado', USR_DEL:'Usuário desativado',
  USR_EXCLUSAO:'Usuário excluído', USR_BLOCK:'Usuário bloqueado', USR_UNBLOCK:'Usuário desbloqueado',
  CAM_ADD:'Câmera cadastrada', CAM_EDIT:'Câmera alterada', CAM_DEL:'Câmera removida', PTZ:'Comando PTZ',
  PESSOA_ADD:'Pessoa cadastrada', PESSOA_EDIT:'Pessoa alterada', PESSOA_DEL:'Pessoa removida',
  BIO_AMOSTRA:'Amostra biométrica', BIO_FUSAO:'Fusão de registros biométricos',
  BIO_RENUMERA:'Identificador biométrico renumerado', CONSULTA_BIO:'Consulta à base biométrica',
  DET_REVISAO:'Detecção revisada', DET_EXCLUSAO:'Detecção excluída',
  ALERTA_ASSUME:'Alerta assumido', ALERTA_FECHA:'Alerta encerrado', ALERTA_EXCLUSAO:'Alerta excluído',
  CONFIG_EDIT:'Configuração alterada', LIMIAR_EDIT:'Limiar alterado',
  EXPORTACAO:'Exportação de dados', EXPURGO:'Expurgo de capturas vencidas'
};
const rotuloAuditoria = c => ROTULO_AUDITORIA[c] || c;

/* Três estados, não dois: bloqueado volta, excluído não. */
const statusUsuario = u => u.excluido_em ? '<span class="chip c-gray">Excluído</span>'
  : u.ativo ? '<span class="chip c-green">Ativo</span>'
  : '<span class="chip c-amber">Bloqueado</span>';

async function renderUsuarios(alvo){
  // Os setores vêm das câmeras: é o mesmo prédio, e manter uma lista à parte
  // faria a unidade do usuário divergir de onde ele de fato trabalha.
  const [perfis, setores] = await Promise.all([API.perfis(), API.setores(true)]);
  let usuarios = await API.usuarios();
  let verExcluidos = false;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Usuários e perfis de acesso</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="usQ" placeholder="Nome, matrícula, e-mail…"></div>
      <button class="btn btn-ghost btn-sm" id="usExc">${ico('eye',14)} Ver excluídos</button>
      <button class="btn btn-primary btn-sm" id="usNovo">${ico('plus',14)} Novo usuário</button>
    </div></div>

  <div class="grid cols-3" style="margin-bottom:18px" id="rolGrid">${carregando()}</div>

  <div class="sec-hd" style="margin-top:22px"><h3>Usuários do sistema</h3></div>
  <div class="panel"><div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl" id="usTbl">
    <thead><tr><th>Usuário</th><th>Perfil</th><th>Setor</th><th>MFA</th>
      <th>Último acesso</th><th>Status</th><th style="width:80px"></th></tr></thead>
    <tbody></tbody></table></div></div></div>`;

  $('#rolGrid').innerHTML = (perfis.perfis||[]).map(pf => {
    const cores = {ADMIN:'c-red', SUPER:'c-amber', AUDITOR:'c-violet'};
    const cor = cores[pf.codigo] || 'c-blue';
    return `<div class="perm-card">
      <div class="perm-head ${cor}">
        <b>${esc(rotuloPerfil(pf.codigo))}</b>
        <span class="mono" style="font-size:10px;opacity:.7">${esc(pf.codigo)}</span>
      </div>
      <div class="perm-list">
        ${pf.permissoes.map(p => `<div class="perm-item">${ico('check',12)} ${esc(PERMS_ROTULO[p]||p)}</div>`).join('')}
      </div>
    </div>`;
  }).join('');

  const pintar = lista => {
    $('#usTbl tbody').innerHTML = lista.map(u => `
      <tr class="user-row ${u.excluido_em ? '' : 'clickable'}" ${u.excluido_em ? '' : `data-u="${esc(u.id)}"`}
          style="${u.excluido_em ? 'opacity:.55' : ''}">
        <td><div class="face-cell"><span class="avatar" style="width:36px;height:36px;font-size:12px">${esc(iniciais(u.nome))}</span>
          <div class="face-meta"><b>${esc(u.nome)}</b><span style="color:var(--txt-3);font-family:var(--f-mono);font-size:11px">${esc(u.email)}</span></div></div></td>
        <td><span class="chip ${u.perfil==='ADMIN'?'c-red':u.perfil==='SUPER'?'c-amber':u.perfil==='AUDITOR'?'c-violet':'c-blue'}">${esc(rotuloPerfil(u.perfil))}</span></td>
        <td style="color:var(--txt-2);font-size:13px">${esc(u.unidade||'—')}</td>
        <td>${u.mfa_ativo ? '<span class="chip c-green">Ativo</span>' : '<span class="chip c-amber">Inativo</span>'}</td>
        <td class="mono" style="font-size:12px">${u.ultimo_acesso ? '<b>'+fmtDT(u.ultimo_acesso).split(' ')[1]+'</b><br><span style="color:var(--txt-4)">'+fmtDT(u.ultimo_acesso).split(' ')[0]+'</span>' : '—'}</td>
        <td>${statusUsuario(u)}</td>
        <td style="text-align:right">${u.excluido_em ? ''
          : `<button class="btn btn-ghost btn-sm btn-icon" data-eu="${esc(u.id)}">${ico('edit',14)}</button>`}</td>
      </tr>`).join('') || `<tr><td colspan="7" style="text-align:center;padding:24px">${vazio('users','Nenhum usuário encontrado.')}</td></tr>`;
    $$('#usTbl [data-eu]').forEach(b => b.onclick = e => { e.stopPropagation(); editarUsuario(b.dataset.eu, usuarios, perfis, setores); });
    $$('#usTbl tr[data-u]').forEach(t => t.onclick = () => editarUsuario(t.dataset.u, usuarios, perfis, setores));
  };

  const filtrados = () => {
    const q = $('#usQ').value.toLowerCase().trim();
    return usuarios.filter(u => !q ||
      [u.nome, u.email, u.matricula, u.unidade].join(' ').toLowerCase().includes(q));
  };

  $('#usQ').oninput = debounce(() => pintar(filtrados()), 200);
  $('#usNovo').onclick = () => editarUsuario(null, usuarios, perfis, setores);
  $('#usExc').onclick = async () => {
    verExcluidos = !verExcluidos;
    // Contas excluídas seguem ocupando e-mail e matrícula; sem poder vê-las,
    // o cadastro de um substituto homônimo falharia sem explicação.
    $('#usExc').innerHTML = ico('eye',14) + (verExcluidos ? ' Ocultar excluídos' : ' Ver excluídos');
    usuarios = await API.usuarios(null, verExcluidos);
    pintar(filtrados());
  };
  pintar(usuarios);
}

function editarUsuario(id, usuarios, perfis, setores){
  const novo = !id;
  const u = novo ? null : usuarios.find(x => x.id === id);
  const listaPerfis = (perfis.perfis || []).map(p => p.codigo);
  // O setor gravado entra na lista mesmo que nenhuma câmera o use mais: sem
  // isso, abrir a ficha de alguém de um setor desativado trocaria o setor
  // dele em silêncio, no primeiro salvamento.
  const listaSetores = [...new Set([...(setores.setores || []),
                                    ...(u && u.unidade ? [u.unidade] : [])])].sort();

  // Sem ícone nestes dois: com "Desbloquear" por extenso, os quatro botões do
  // rodapé encostam nos 560px do modal e a linha quebra.
  const acoes = novo ? '' :
    `<button class="btn btn-soft" id="uBloq">${u.ativo ? 'Bloquear' : 'Desbloquear'}</button>
     <button class="btn btn-danger" id="uDel">Excluir</button>`;

  abrirModal(`
    <div class="modal-hd"><h3>${novo ? 'Novo usuário' : 'Editar usuário'}</h3>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="form-grid">
        <div class="field full"><label for="uNome">Nome completo</label>
          <input class="inp" id="uNome" value="${u?esc(u.nome):''}" placeholder="Ex.: Priscila Tavares"></div>
        <div class="field"><label for="uMat">Matrícula</label>
          <input class="inp mono" id="uMat" value="${u?esc(u.matricula):''}" placeholder="PA-000000"></div>
        <div class="field"><label for="uMail">E-mail funcional</label>
          <input class="inp mono" id="uMail" value="${u?esc(u.email):''}" placeholder="nome@exemplo.com.br"></div>
        <div class="field"><label for="uPerfil">Perfil de acesso</label>
          <select class="inp" id="uPerfil">${listaPerfis.map(p =>
            `<option value="${p}" ${u&&u.perfil===p?'selected':''}>${esc(rotuloPerfil(p))}</option>`).join('')}</select></div>
        <div class="field"><label for="uSetor">Setor</label>
          <select class="inp" id="uSetor">${listaSetores.map(x =>
            `<option ${u&&u.unidade===x?'selected':''}>${esc(x)}</option>`).join('')}</select></div>
      </div>
      <div class="eyebrow" style="margin:17px 0 4px">Permissões efetivas</div>
      <div id="permBox" style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px"></div>
      <div class="sw-row" data-usw="mfa_ativo"><div class="bd"><b>Autenticação em dois fatores</b>
        <span>Recomendada para todos os perfis com permissão de edição</span></div>
        <span class="sw ${(!u||u.mfa_ativo)?'on':''}"></span></div>
      ${novo ? `<div class="note" style="margin-top:14px">${ico('key',15)}<div>Um link de primeiro acesso será enviado para o e-mail informado. A pessoa define a própria senha — ninguém mais a conhece. O link vale 30 minutos e serve uma vez.</div></div>` : ''}
      ${u && !u.ativo ? `<div class="note" style="margin-top:14px">${ico('lock',15)}<div><b>Conta bloqueada.</b> ${esc(u.nome)} não consegue autenticar. Desbloquear devolve o acesso com as mesmas permissões.</div></div>` : ''}
      ${u && u.ativo ? `<div class="note" style="margin-top:14px;align-items:flex-start">${ico('key',15)}
        <div style="flex:1">
          <b>Esqueceu a senha?</b> Emita um link de redefinição e entregue a
          ${esc(u.nome.split(' ')[0])} pelo canal que você já usa — pessoalmente,
          por rádio ou telefone. Vale 30 minutos e serve uma vez só.
          <div style="margin-top:10px"><button class="btn btn-soft btn-sm" id="uLink">${ico('key',13)} Emitir link</button></div>
        </div></div>` : ''}
    </div>
    <div class="modal-ft">
      ${acoes}
      <span style="flex:1"></span>
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="uSave">${ico('check',14)} ${novo ? 'Criar usuário' : 'Salvar'}</button>
    </div>`);

  const flags = {mfa_ativo: !u || u.mfa_ativo};
  $$('[data-usw]').forEach(r => r.onclick = () => {
    const k = r.dataset.usw; flags[k] = !flags[k];
    r.querySelector('.sw').classList.toggle('on', flags[k]);
  });
  const pintarPerms = () => {
    const pf = (perfis.perfis || []).find(p => p.codigo === $('#uPerfil').value);
    $('#permBox').innerHTML = (pf ? pf.permissoes : []).map(p => `<span class="chip c-blue">${esc(p)}</span>`).join('');
  };
  $('#uPerfil').onchange = pintarPerms;
  pintarPerms();

  const bb = $('#uBloq');
  if(bb) bb.onclick = async () => {
    try{
      if(!u.ativo){
        if(!await confirmar('Desbloquear usuário',
          `Devolver o acesso de <b>${esc(u.nome)}</b>?<br><br>` +
          `A conta volta a autenticar com o perfil <b>${esc(rotuloPerfil(u.perfil))}</b> e as mesmas permissões de antes.`,
          {rotulo:'Desbloquear'})) return;
        await API.desbloquearUsuario(u.id);
        fecharModal();
        toast('Usuário desbloqueado', `${u.nome} voltou a ter acesso.`, 'ok');
      }else{
        const motivo = await confirmar('Bloquear usuário',
          `Suspender o acesso de <b>${esc(u.nome)}</b>?<br><br>` +
          `A conta deixa de autenticar e a sessão aberta cai na próxima requisição. ` +
          `O cadastro, o perfil e o histórico ficam como estão — isto se desfaz a qualquer momento.`,
          {rotulo:'Bloquear', exigeTexto:'Motivo do bloqueio'});
        if(!motivo) return;
        await API.bloquearUsuario(u.id, motivo);
        fecharModal();
        toast('Usuário bloqueado', `${u.nome} está sem acesso.`, 'warn');
      }
      irPara('users');
    }catch(e){ erroToast(e); }
  };

  const bd = $('#uDel');
  if(bd) bd.onclick = async () => {
    const motivo = await confirmar('Excluir usuário',
      `Excluir <b>${esc(u.nome)}</b> — ${esc(u.email)} — em definitivo?<br><br>` +
      `A conta perde o acesso e sai da lista. O cadastro <b>não é apagado do banco</b>: ` +
      `a trilha de auditoria é encadeada por hash e remover a linha acusaria adulteração ` +
      `em tudo que ${esc(u.nome)} assinou. Por isso o e-mail e a matrícula seguem ocupados.` +
      `<br><br>Isto não se desfaz — para suspender o acesso de forma reversível, use <b>Bloquear</b>.`,
      {rotulo:'Excluir em definitivo', perigo:true, exigeTexto:'Motivo da exclusão'});
    if(!motivo) return;
    try{
      await API.excluirUsuario(u.id, motivo);
      fecharModal();
      toast('Usuário excluído', `${u.nome} não tem mais acesso ao sistema.`, 'warn');
      irPara('users');
    }catch(e){ erroToast(e); }
  };

  /* Emitir o link é entregar o acesso à conta a quem o receber, então o aviso
     diz isso com todas as letras: o risco não está em gerar, está em entregar
     à pessoa errada — e quem entrega é o administrador, fora do sistema, onde
     nada mais consegue conferir. */
  const bl = $('#uLink');
  if(bl) bl.onclick = async () => {
    const primeiro = esc(u.nome.split(' ')[0]);
    const motivo = await confirmar('Emitir link de redefinição',
      `Gerar um link de troca de senha para <b>${esc(u.nome)}</b>?<br><br>` +
      `Quem abrir o link define a senha da conta <b>sem precisar da senha atual</b>. ` +
      `Entregue só a ${primeiro}, e por um canal em que você tenha certeza de com quem fala.` +
      `<br><br>Vale 30 minutos, serve uma vez e cancela qualquer link anterior ainda aberto. ` +
      `A emissão fica na trilha de auditoria com o seu nome.`,
      {rotulo:'Emitir link', exigeTexto:'Motivo da emissão'});
    if(!motivo) return;

    bl.disabled = true;
    try{
      const r = await API.linkRedefinicao(u.id, motivo);
      const minutos = Math.round((r.expira_em_segundos || 1800) / 60);

      /* Modal próprio, e não uma caixa dentro da ficha: `abrirModal` troca o
         conteúdo do mesmo contêiner, então a confirmação acima já desmontou a
         ficha de edição — escrever num elemento dela daria em `null`. */
      abrirModal(`
        <div class="modal-hd"><h3>Link emitido</h3><span class="sp" style="flex:1"></span>
          <button class="x" data-close>${ico('x',17)}</button></div>
        <div class="modal-bd">
          <div class="note" style="margin-bottom:15px;align-items:flex-start">${ico('key',15)}
            <div>Entregue a <b>${esc(u.nome)}</b>. Quem abrir este link define a senha
            da conta — ele vale por si só, como uma chave.</div></div>
          <div class="field">
            <label for="uLinkTxt">Link de redefinição — expira em ${minutos} minutos</label>
            <div style="display:flex;gap:6px">
              <input class="inp mono" id="uLinkTxt" readonly value="${esc(r.link)}"
                     style="flex:1;min-width:0;font-size:11px">
              <button class="btn btn-primary" id="uLinkCopy">Copiar</button>
            </div>
          </div>
        </div>
        <div class="modal-ft"><span style="flex:1"></span>
          <button class="btn btn-ghost" data-close>Fechar</button></div>`);

      const campo = $('#uLinkTxt');
      campo.onclick = () => campo.select();
      $('#uLinkCopy').onclick = async () => {
        try{
          await navigator.clipboard.writeText(campo.value);
        }catch(e){
          // `clipboard` exige contexto seguro e permissão; em http sem ser
          // localhost ela nem existe. A seleção manual continua valendo, e
          // `execCommand` é obsoleto mas é o que ainda funciona ali.
          campo.select();
          try{ document.execCommand('copy'); }catch(e2){}
        }
        toast('Link copiado', `Entregue a ${u.nome.split(' ')[0]} — vale ${minutos} minutos.`, 'ok');
      };
      campo.select();
    }catch(e){ erroToast(e); }
    finally{ bl.disabled = false; }
  };

  $('#uSave').onclick = async () => {
    const dados = {
      nome: $('#uNome').value.trim(),
      email: $('#uMail').value.trim().toLowerCase(),
      matricula: $('#uMat').value.trim(),
      perfil: $('#uPerfil').value,
      unidade: $('#uSetor').value,
      ...flags
    };
    if(dados.nome.length < 4){ toast('Nome inválido', 'Informe o nome completo.', 'err'); return; }
    if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(dados.email)){
      toast('E-mail inválido', 'Use o e-mail funcional institucional.', 'err'); return;
    }
    const btn = $('#uSave'); btn.disabled = true;
    try{
      if(novo){
        const r = await API.criarUsuario(dados);
        fecharModal();
        const pa = r.primeiro_acesso;
        if(pa && pa.envio === 'enviado'){
          toast('Usuário criado', `Link de primeiro acesso enviado para ${dados.email}.`, 'ok', 6000);
          irPara('users');
        }else if(pa){
          /* O e-mail não saiu, mas a conta existe. Sem o link aqui ela
             nasceria inacessível e alguém teria de emitir outro à mão —
             então ele aparece, uma vez, para entrega por canal seguro. */
          const motivo = pa.envio === 'nao_configurado'
            ? 'O envio de e-mail não está configurado nesta instalação.'
            : `O e-mail não saiu: ${esc(pa.erro || 'falha no servidor de e-mail')}`;
          abrirModal(`
            <div class="modal-hd"><h3>Usuário criado — entregue o link</h3><span class="sp" style="flex:1"></span>
              <button class="x" data-close>${ico('x',17)}</button></div>
            <div class="modal-bd">
              <p style="margin:0 0 14px;color:var(--txt-2)">${motivo} Entregue o
                endereço abaixo a <b>${esc(dados.nome)}</b> por canal seguro — pessoalmente,
                por rádio ou telefone. Ele não será exibido de novo.</p>
              <div class="demo-box"><div class="eyebrow">Link de primeiro acesso</div>
                <div class="mono" style="font-size:12px;color:var(--acc-2);padding:8px 0;user-select:all;word-break:break-all">${esc(pa.link)}</div></div>
              <div class="note" style="margin-top:14px">${ico('info',15)}<div>Vale
                ${Math.round((pa.expira_em_segundos || 1800) / 60)} minutos e serve uma vez só.
                A senha que ela escolher não é conhecida por mais ninguém.</div></div>
            </div>
            <div class="modal-ft"><button class="btn btn-primary" data-close>Entendi</button></div>`);
          $$('[data-close]').forEach(b => b.onclick = () => { fecharModal(); irPara('users'); });
        }else{
          toast('Usuário criado', dados.nome, 'ok');
          irPara('users');
        }
      }else{
        await API.salvarUsuario(u.id, dados);
        fecharModal();
        toast('Usuário atualizado', `${dados.nome} · ${rotuloPerfil(dados.perfil)}`, 'ok');
        irPara('users');
      }
    }catch(e){ erroToast(e); }
    finally{ btn.disabled = false; }
  };
}

/* ==========================================================
   AUDITORIA
   ========================================================== */
async function renderAuditoria(alvo){
  const acoes = await API.acoesAuditoria();

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Trilha de auditoria</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="auQ" value="${esc(FILTRO.audit.q)}" placeholder="Usuário, ação, alvo, IP…"></div>
      <select class="inp" id="auAcao"><option value="">Todas as ações</option>
        ${acoes.map(a => `<option value="${esc(a.acao)}" ${FILTRO.audit.acao===a.acao?'selected':''}>${esc(rotuloAuditoria(a.acao))} (${a.total})</option>`).join('')}</select>
      <button class="btn btn-ghost btn-sm" id="auVerificar">${ico('shield',14)} Verificar cadeia</button>
      ${API.pode('export') ? `<button class="btn btn-ghost btn-sm" id="auExp">${ico('dl',14)} Exportar</button>` : ''}
    </div></div>
  <div id="auVerif"></div>
  <div class="note" style="margin-bottom:14px">${ico('lock',15)}<div>Registros encadeados por hash e somente-inserção — uma trigger no banco recusa <span class="mono">UPDATE</span> e <span class="mono">DELETE</span>. Consulta de biométricos, exportação ou mudança de limiar fica vinculada à matrícula do operador.</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="auCorpo">${carregando()}</div></div></div>`;

  /* Quantos registros o filtro atual alcança. Guardado da última consulta
     para o diálogo de exportação poder dizer o tamanho do que vai sair —
     exportar às cegas uma trilha de milhares de linhas é o tipo de coisa que
     só se descobre quando o arquivo chega. */
  let ultimoTotal = null;

  const carregar = async () => {
    const corpo = $('#auCorpo');
    FILTRO.audit = {q: $('#auQ').value.trim(), acao: $('#auAcao').value};
    try{
      const r = await API.auditoria({
        pagina: PAG.audit, por_pagina: 30,
        q: FILTRO.audit.q || undefined, acao: FILTRO.audit.acao || undefined
      });
      ultimoTotal = r.total;
      const itens = r.itens || [];

      const iconoPorAcao = (acao) => {
        if(acao.includes('LOGIN')) return 'key';
        if(acao.includes('LOGOUT')) return 'logout';
        if(acao.includes('DELETE')) return 'trash';
        if(acao.includes('UPDATE')) return 'edit';
        if(acao.includes('EXPORT')) return 'dl';
        if(acao.includes('CADEIA')) return 'shield';
        return 'log';
      };

      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr>
          <th style="width:8px"></th>
          <th>Data / hora</th>
          <th>Ação</th>
          <th>Descrição</th>
          <th>Usuário</th>
          <th>Alvo</th>
          <th style="width:80px">IP</th></tr></thead>
        <tbody>${itens.map(a => `<tr class="audit-row">
          <td style="padding:0"><div style="height:100%;min-height:52px;width:3px;background:${
            a.severidade === '4' ? 'var(--red)' :
            a.severidade === '3' ? 'var(--amber)' :
            a.severidade === '2' ? 'var(--green)' : 'var(--acc)'
          }"></div></td>
          <td class="mono" style="font-size:12px;white-space:nowrap"><b>${fmtDT(a.ocorrido_em).split(' ')[0]}</b><br><span style="color:var(--txt-4)">${fmtDT(a.ocorrido_em).split(' ')[1]}</span></td>
          <td><div style="display:flex;align-items:center;gap:7px"><span style="width:20px;text-align:center">${ico(iconoPorAcao(a.acao),14)}</span><div><b style="white-space:nowrap">${esc(rotuloAuditoria(a.acao))}</b><br><span class="td-id">${esc(a.acao)}</span></div></div></td>
          <td style="max-width:280px"><span style="color:var(--txt-2);font-size:13px">${esc(a.descricao || '—')}</span></td>
          <td><b style="display:block">${esc(a.usuario_nome||'—')}</b><span class="mono" style="font-size:11px;color:var(--txt-3)">${esc((a.usuario_email||'').split('@')[0]||'')}</span></td>
          <td class="mono" style="font-size:12px;color:var(--txt-3);max-width:120px;overflow:hidden;text-overflow:ellipsis" title="${esc(a.alvo||'')}">${esc(a.alvo||'—')}</td>
          <td class="mono" style="font-size:11px;color:var(--txt-4)">${esc(a.ip||'—')}</td>
        </tr>`).join('') || `<tr><td colspan="7" style="text-align:center;padding:24px">${vazio('log','Nenhum registro.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'audit')}`;
      ligarPager(corpo, 'audit', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#auQ').oninput = debounce(() => { PAG.audit = 1; carregar(); }, 350);
  $('#auAcao').onchange = () => { PAG.audit = 1; carregar(); };

  $('#auVerificar').onclick = async () => {
    const box = $('#auVerif');
    box.innerHTML = `<div class="alert alert-info" style="margin-bottom:13px">${ico('info',15)}<div>Recalculando a cadeia…</div></div>`;
    try{
      const v = await API.verificarCadeia();
      /* Três veredictos, não dois. "Comprometida" em vermelho é para linha
         reescrita — e só. Esta trilha tem 83 elos bifurcados por gravação
         simultânea e 10 vínculos zerados pela exclusão de uma conta: nada
         disso é adulteração, e pintar tudo de vermelho ensina o operador a
         ignorar o aviso. Quando ele ignorar o aviso de verdade, a trilha
         deixou de servir para o que existe. */
      const adulteradas = (v.conteudo_divergente||0) - (v.vinculo_de_usuario_perdido||0);
      const nivel = v.integra ? 'ok' : (adulteradas > 0 ? 'err' : 'warn');
      const titulo = v.integra ? 'Cadeia íntegra'
                   : (adulteradas > 0 ? 'Cadeia comprometida' : 'Cadeia íntegra, com ressalvas');
      box.innerHTML = `<div class="alert alert-${nivel}" style="margin-bottom:13px">
        ${ico(nivel === 'ok' ? 'check' : 'alert', 15)}<div><b>${titulo}</b><br>${esc(v.mensagem)}</div></div>`;
    }catch(e){
      box.innerHTML = `<div class="alert alert-err" style="margin-bottom:13px">${ico('alert',15)}<div>${esc(e.message)}</div></div>`;
    }
  };

  const be = $('#auExp');
  if(be) be.onclick = async () => {
    /* O que vai no arquivo é o que está na tela. Dizer isso *antes* evita o
       engano mais caro desta tela: filtrar por uma ação, exportar e entregar
       a corregedoria um arquivo com outro conteúdo — ou o contrário, achar
       que levou tudo e ter levado um recorte. */
    const filtro = {q: FILTRO.audit.q || undefined, acao: FILTRO.audit.acao || undefined};
    const recorte = [
      filtro.acao ? `ação <b>${esc(rotuloAuditoria(filtro.acao))}</b>` : '',
      filtro.q ? `busca por <b>${esc(filtro.q)}</b>` : '',
    ].filter(Boolean).join(' e ');
    const quantas = ultimoTotal == null ? '' : ` <b>${fmtNum(ultimoTotal)}</b> registro(s)`;

    const fin = await confirmar('Exportar trilha em PDF',
      (recorte
        ? `Vai no documento o que está filtrado na tela:${quantas} com ${recorte}.`
        : `Vai no documento a <b>trilha completa</b>, sem filtro —${quantas}.`)
      + `<br>O PDF sai com capa de identificação, selo de integridade da cadeia
         e numeração de páginas — pronto para juntar a processo.`
      + '<br><br>A exportação em si será registrada na trilha, com a finalidade informada.',
      {rotulo:'Exportar PDF', exigeTexto:'Finalidade da exportação'});
    if(!fin) return;

    /* O botão sai do ar enquanto o documento é montado. São segundos de
       espera numa trilha grande — montar mil e trezentas linhas leva uns
       quatro —, e dois cliques gerariam dois documentos e duas linhas na
       própria trilha. */
    const antes = be.innerHTML;
    be.disabled = true;
    be.innerHTML = `${ico('dl',14)} Gerando o PDF…`;
    try{
      const nome = await API.exportarAuditoria(fin, filtro);
      toast('Trilha exportada', `Documento ${nome} salvo nos downloads.`, 'ok');
      // A exportação virou uma linha nova na trilha; a tela mostra isso.
      PAG.audit = 1;
      await carregar();
    }catch(e){
      toast('Exportação não concluída', e.message, 'err', 8000);
    }finally{
      be.disabled = false;
      be.innerHTML = antes;
    }
  };

  await carregar();
}

/* ==========================================================
   CONFIGURAÇÕES
   ========================================================== */
const GRUPOS_CFG = [
  ['retencao', 'Retenção e privacidade', 'shield'],
  ['acesso',   'Acesso e sessão',        'key'],
  ['deteccao', 'Detecção',               'gauge'],
];

/* Um campo por parâmetro, com a faixa que o servidor declarou. O `min`/`max`
   aqui é conveniência de digitação — quem recusa de verdade é o servidor,
   porque o navegador não é lugar para guardar regra. */
function campoCfg(p){
  const id = 'cfg_' + p.chave;
  if(p.tipo === 'perfis'){
    const atuais = String(p.valor || '').split(',').map(x => x.trim()).filter(Boolean);
    return `<div class="field full" data-cfg="${esc(p.chave)}" data-tipo="perfis">
      <label>${esc(p.rotulo)}</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 5px">
        ${(p.opcoes||[]).map(o => `<span class="chip ${atuais.includes(o)?'c-blue':'c-gray'} clickable"
          data-perfil="${esc(o)}" style="cursor:pointer;user-select:none">${esc(rotuloPerfil(o))}</span>`).join('')}
      </div>
      <span class="hint">${esc(p.ajuda)}</span></div>`;
  }
  const passo = p.tipo === 'float' ? '0.01' : '1';
  return `<div class="field" data-cfg="${esc(p.chave)}" data-tipo="${esc(p.tipo)}">
    <label for="${id}">${esc(p.rotulo)}${p.unidade ? ` <span style="color:var(--txt-3);font-weight:400">(${esc(p.unidade)})</span>` : ''}</label>
    <input class="inp mono" id="${id}" type="number" value="${esc(String(p.valor))}"
           min="${p.minimo}" max="${p.maximo}" step="${passo}">
    <span class="hint">${esc(p.ajuda)} <b>Faixa: ${p.minimo} a ${p.maximo}.</b></span></div>`;
}

async function renderConfiguracoes(alvo){
  const [info, saude, cfg] = await Promise.all([API.info(), API.saude(), API.configuracao()]);
  const c = info.config || {};

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Configurações do sistema</h2></div>

  <div class="split" style="align-items:flex-start;margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Motores de análise</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Reconhecimento facial</dt><dd>${info.modelos && info.modelos.face ? '<span class="chip c-green">InsightFace carregado</span>' : '<span class="chip c-amber">Modelo não instalado</span>'}</dd>
          <dt>Detecção de objetos</dt><dd>${info.modelos && info.modelos.yolo ? '<span class="chip c-green">YOLO carregado</span>' : '<span class="chip c-amber">Modelo não instalado</span>'}</dd>
          <dt>Limiar global facial</dt><dd class="mono">${Math.round((c.limiar_facial||0)*100)}%</dd>
          <dt>Limiar global objeto</dt><dd class="mono">${Math.round((c.limiar_objeto||0)*100)}%</dd>
          <dt>Dimensão do vetor</dt><dd class="mono">${c.embedding_dim || 512}-D</dd>
          <dt>Dispositivo</dt><dd class="mono">${esc(c.device||'cpu')}</dd>
        </dl>
        ${!(info.modelos && info.modelos.face) ? `<div class="note" style="margin-top:14px">${ico('info',15)}<div>Sem os modelos instalados, a busca por imagem fica indisponível — o restante do sistema opera normalmente com o simulador. Para habilitar:<br><span class="mono" style="color:var(--acc-2)">pip install insightface onnxruntime ultralytics</span></div></div>` : ''}
        <div class="note" style="margin-top:12px">${ico('alert',15)}<div>O limiar é ajustado <b>por câmera</b>, na tela de configuração de cada uma. Limiares baixos aumentam falsos positivos — e o custo de um falso positivo recai sobre uma pessoa inocente.</div></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Privacidade e retenção</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Retenção de não identificadas</dt><dd>${c.retencao_dias || 90} dias</dd>
          <dt>Mascaramento de PII</dt><dd>${esc((c.mascarar_para||[]).join(', ') || 'nenhum perfil')}</dd>
          <dt>Expiração de senha</dt><dd>${c.senha_expira_dias || 90} dias</dd>
          <dt>Tentativas de login</dt><dd>${c.max_tentativas || 5} antes do bloqueio</dd>
          <dt>Sessão</dt><dd>${c.sessao_minutos || 60} minutos</dd>
        </dl>
        ${API.pode('settings') ? `<button class="btn btn-soft btn-sm" id="cfgExpurgo" style="margin-top:14px">${ico('trash',13)} Expurgar capturas vencidas</button>` : ''}
        <div class="note" style="margin-top:14px">${ico('shield',15)}<div>Tratamento de dado biométrico exige <b>base legal específica</b> na LGPD, aviso às pessoas monitoradas e avaliação de impacto. O uso em ambiente de trabalho depende ainda do que a norma interna e a legislação trabalhista permitem.</div></div>
      </div>
    </div>
  </div>

  <div class="panel" style="margin-bottom:14px">
    <div class="panel-hd"><h3>Ajustar parâmetros</h3>
      <span class="sub">valem na requisição seguinte, sem reiniciar</span>
      <span class="sp"></span>
      <button class="btn btn-primary btn-sm" id="cfgSalvar">${ico('check',14)} Salvar alterações</button></div>
    <div class="panel-bd">
      ${GRUPOS_CFG.map(([g, titulo, icone]) => {
        const doGrupo = (cfg.parametros||[]).filter(p => p.grupo === g);
        if(!doGrupo.length) return '';
        return `<div class="eyebrow" style="display:flex;align-items:center;gap:6px;margin:4px 0 9px">
            <span style="color:var(--acc)">${ico(icone,14)}</span>${esc(titulo)}</div>
          <div class="form-grid" style="margin-bottom:18px">${doGrupo.map(campoCfg).join('')}</div>`;
      }).join('')}

      <div class="eyebrow" style="display:flex;align-items:center;gap:6px;margin:4px 0 9px">
        <span style="color:var(--txt-3)">${ico('lock',14)}</span>Fixos — exigem reinício</div>
      <dl class="kv" style="margin-bottom:14px">
        ${(cfg.fixos||[]).map(f => `<dt>${esc(f.rotulo)}</dt>
          <dd class="mono">${esc(String(f.valor))}
            <span style="color:var(--txt-4);font-size:11px"> · ${esc(f.variavel)}</span></dd>`).join('')}
      </dl>

      <div class="note">${ico('info',15)}<div>Os fixos são lidos quando o processo sobe: mudá-los é no <span class="mono">.env</span> seguido de reinício. Gravá-los aqui mostraria um número que o sistema não está usando. Cada alteração dos ajustáveis fica na trilha de auditoria, com o valor anterior e o novo.</div></div>
    </div>
  </div>

  <div class="split" style="align-items:flex-start;margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Infraestrutura</h3>
        <span class="sp"></span><span class="chip ${saude.saudavel?'c-green':'c-red'}">${saude.saudavel?'Saudável':'Degradado'}</span></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Versão</dt><dd class="mono">SEGURANÇA ${esc(info.versao||'')}</dd>
          <dt>Modo</dt><dd class="mono">${esc(info.modo||'')}</dd>
          <dt>Banco</dt><dd class="mono">${esc(saude.componentes.banco)}</dd>
          <dt>Barramento</dt><dd class="mono">${esc(saude.componentes.eventos||'—')}</dd>
          <dt>WebSockets ativos</dt><dd class="mono">${saude.websockets_ativos}</dd>
          <dt>Simulador</dt><dd>${info.simulador ? `<span class="chip c-blue">Ativo · ${fmtNum(info.simulador_gerados||0)} eventos</span>` : '<span class="chip c-gray">Desligado</span>'}</dd>
          <dt>Busca vetorial</dt><dd class="mono">${esc(info.busca_vetorial||'—')}</dd>
        </dl>
      </div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Estatísticas</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Câmeras</dt><dd class="mono">${fmtNum((info.estatisticas||{}).cameras||0)}</dd>
          <dt>Registros biométricos</dt><dd class="mono">${fmtNum((info.estatisticas||{}).pessoas||0)}</dd>
          <dt>Detecções</dt><dd class="mono">${fmtNum((info.estatisticas||{}).deteccoes||0)}</dd>
          <dt>Alertas</dt><dd class="mono">${fmtNum((info.estatisticas||{}).alertas||0)}</dd>
          <dt>Linhas de auditoria</dt><dd class="mono">${fmtNum((info.estatisticas||{}).auditoria||0)}</dd>
          <dt>Usuários</dt><dd class="mono">${fmtNum((info.estatisticas||{}).usuarios||0)}</dd>
        </dl>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-hd"><h3>Como ligar às câmeras reais</h3><span class="sub">o que muda ao sair da demonstração</span></div>
    <div class="panel-bd">
      <p class="hint" style="max-width:76ch;margin:0 0 13px">A imagem que você vê no mosaico é gerada no navegador. Para operar com equipamentos reais, cada bloco troca de origem sem alteração na interface:</p>
      <div class="grid cols-3">
        ${[
          ['Vídeo ao vivo','As câmeras publicam RTSP; um gateway (MediaMTX) converte para WebRTC. Cada tile troca o &lt;canvas&gt; por um &lt;video&gt; apontado a <span class="mono">GET /stream/{codigo}</span>.','live'],
          ['Reconhecimento facial','RetinaFace para detectar e ArcFace para o vetor 512-D. Instale <span class="mono">insightface</span> e a busca por imagem passa a funcionar.','face'],
          ['Objetos e ações','YOLO v8 para objetos e análise de trajetórias para comportamento, com ByteTrack mantendo a identidade entre quadros.','box'],
          ['Banco','Troque <span class="mono">DATABASE_URL</span> para PostgreSQL e a busca passa a usar índice HNSW do pgvector em vez de força bruta.','db'],
          ['Barramento','Defina <span class="mono">REDIS_URL</span> e vários workers e instâncias da API passam a compartilhar o mesmo fluxo de eventos.','cog'],
          ['Worker','<span class="mono">python -m worker.run --todas</span> lê o RTSP, roda os modelos e envia para <span class="mono">POST /deteccoes/ingest</span> — a mesma rota do simulador.','gauge']
        ].map(([t,d,i]) => `<div class="id-card" style="min-width:0">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><span style="color:var(--acc)">${ico(i,15)}</span><b style="font-size:13px">${t}</b></div>
          <p class="hint" style="margin:0">${d}</p></div>`).join('')}
      </div>
    </div>
  </div>`;

  $$('[data-cfg][data-tipo="perfis"] [data-perfil]').forEach(ch => ch.onclick = () => {
    const ligado = ch.classList.toggle('c-blue');
    ch.classList.toggle('c-gray', !ligado);
  });

  $('#cfgSalvar').onclick = async () => {
    const valores = {};
    (cfg.parametros||[]).forEach(p => {
      const campo = $(`[data-cfg="${p.chave}"]`);
      if(!campo) return;
      if(p.tipo === 'perfis'){
        const escolhidos = $$('[data-perfil]', campo)
          .filter(c => c.classList.contains('c-blue')).map(c => c.dataset.perfil);
        const atual = String(p.valor || '').split(',').map(x => x.trim()).filter(Boolean);
        if(escolhidos.join(',') !== atual.join(',')) valores[p.chave] = escolhidos.join(',');
      }else{
        const bruto = $('input', campo).value.trim();
        if(bruto !== '' && Number(bruto) !== Number(p.valor)) valores[p.chave] = Number(bruto);
      }
    });

    if(!Object.keys(valores).length){ toast('Nada a salvar', 'Nenhum parâmetro foi alterado.', 'info'); return; }

    const btn = $('#cfgSalvar'); btn.disabled = true;
    try{
      const r = await API.salvarConfiguracao(valores);
      toast('Configuração salva',
        `${r.alterados.length} parâmetro(s) em vigor a partir de agora.`, 'ok');
      irPara('settings');
    }catch(e){ erroToast(e); }
    finally{ btn.disabled = false; }
  };

  const be = $('#cfgExpurgo');
  if(be) be.onclick = async () => {
    const ok = await confirmar('Expurgar capturas vencidas',
      'Remove capturas faciais <b>sem correspondência</b> cujo prazo de retenção venceu. Capturas vinculadas a alerta ou já revisadas são preservadas.',
      {rotulo:'Expurgar', perigo:true});
    if(!ok) return;
    try{
      const r = await API.expurgar();
      toast('Expurgo concluído', `${r.removidos} registro(s) removido(s) — prazo de ${r.prazo_dias} dias.`, 'ok');
    }catch(e){ erroToast(e); }
  };
}
