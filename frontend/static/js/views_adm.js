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
  const ab = resumo.abertos || 0, ea = resumo.em_andamento || 0, rs = resumo.resolvidos || 0;
  const total = ab + ea + rs, crit = resumo.criticos_pendentes || 0;

  /* Cartão que filtra a lista pelo status; clicar de novo volta a mostrar todos. */
  const kpi = (st, cls, lbl, icone, val, sub, extra) => `<button type="button" class="kpi cm-kpi ${cls} ${f.status === st ? 'on' : ''}" data-st="${st}"
      title="${f.status === st ? 'Mostrar todos os alertas' : `Listar só os ${esc(lbl.toLowerCase())}`}">
    <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
    <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></button>`;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Central de alertas</h2><span class="cm-sub">ocorrências que exigem tratamento</span><span class="sp"></span></div>

  <div class="dash-grid dash-kpis">
    ${kpi('ABERTO', 'k-red', 'Abertos', 'bell', fmtNum(ab), `${pctCurto(ab, total)} do total · aguardando triagem`, dashBarra(total ? ab / total : 0, 'var(--red)'))}
    ${kpi('EM_ANDAMENTO', 'k-amber', 'Em andamento', 'clock', fmtNum(ea), 'com responsável atribuído', dashBarra(total ? ea / total : 0, 'var(--amber)'))}
    ${kpi('RESOLVIDO', 'k-green', 'Resolvidos', 'check', fmtNum(rs), `${pctCurto(rs, total)} com desfecho registrado`, dashBarra(total ? rs / total : 0, 'var(--green)'))}
    <div class="kpi al-kpi-tempo">
      <div class="kpi-top"><span class="kpi-lbl">Tempo médio de resposta</span><span class="kpi-ico">${ico('gauge',15)}</span></div>
      <div class="kpi-val">${resumo.tempo_medio_resposta_s ? duracao(resumo.tempo_medio_resposta_s) : '—'}</div>
      <div class="kpi-delta nt">${crit
        ? `<span class="al-crit">${ico('alert',12)} ${fmtNum(crit)} crítico(s) pendente(s)</span>`
        : 'nenhum crítico pendente'}</div>
    </div>
  </div>

  <div class="cm-barra fc-barra">
    <div class="segm" id="alSeg">
      ${[['', 'Todos', total], ['ABERTO', 'Abertos', ab], ['EM_ANDAMENTO', 'Em curso', ea], ['RESOLVIDO', 'Resolvidos', rs]].map(([k, l, q]) =>
        `<button data-s="${k}" class="${(f.status || '') === k ? 'on' : ''}">${l} <em>${fmtNum(q)}</em></button>`).join('')}
    </div>
    <select class="inp" id="alSev"><option value="">Qualquer severidade</option>
      ${[4,3,2].map(s => `<option value="${s}" ${f.severidade==String(s)?'selected':''}>${SEVERIDADE[s]}</option>`).join('')}</select>
    <select class="inp" id="alCam"><option value="">Todas as câmeras</option>${opcoesCameras(null, f.cam)}
      <option value="VIDEO-TESTE" ${f.cam === 'VIDEO-TESTE' ? 'selected' : ''}>Vídeo de teste</option></select>
    <div class="periodo">
      <label for="alDe">Período</label>
      <input class="inp" id="alDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || hoje)}">
      <span class="sep">até</span>
      <input class="inp" id="alAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${hoje}">
      <button class="btn btn-ghost btn-sm" id="alLimpar" ${f.de||f.ate?'':'hidden'}>Limpar</button>
    </div>
    <span class="sp"></span>
    <span class="cm-cont" id="alCont"></span>
  </div>

  <div class="panel"><div class="panel-bd tight"><div id="alCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#alCorpo');
    corpo.innerHTML = carregando();
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
      const cont = $('#alCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} alerta(s)`;
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl al-tbl">
        <thead><tr><th style="width:74px">Captura</th><th>Alerta</th><th>Severidade</th><th>Tipo</th><th>Câmera</th>
          <th>Disparado</th><th>Status</th><th style="width:104px"></th></tr></thead>
        <tbody>${itens.map(a => {
          const st = STATUS_ALERTA[a.status] || {};
          const img = imagemDoAlerta(a);
          const cor = SEV_COR[a.severidade] || 'var(--txt-3)';
          const det = a.detalhe || '';
          return `<tr class="clickable al-sev-${a.severidade}" data-a="${esc(a.codigo)}" style="--sev:${cor}">
            <td><div class="fc-cap" style="--cap:${cor}">${img
              ? `<canvas data-snap="${snapAttr(img, img.fonte)}" width="46"></canvas>`
              : `<span class="al-semimg">${ico('bell', 16)}</span>`}</div></td>
            <td><div class="al-tit"><b>${esc(a.titulo)}</b>
              <span class="mono">${esc(a.codigo)}</span>
              ${det ? `<span class="fc-sub" title="${esc(det)}">${esc(det.slice(0, 70))}${det.length > 70 ? '…' : ''}</span>` : ''}</div></td>
            <td><span class="ob-sev" style="--sev:${cor}"><i></i>${esc(SEVERIDADE[a.severidade] || '')}</span></td>
            <td><span class="chip ${COR_TIPO_ALERTA[a.tipo] || 'c-gray'}">${esc(ROTULO_TIPO_ALERTA[a.tipo] || a.tipo)}</span></td>
            <td><b class="fc-cam">${esc(a.camera?a.camera.codigo:'')}</b><span class="fc-sub">${esc((a.camera?a.camera.nome:'').slice(0,24))}</span></td>
            <td><span class="fc-hora mono">${fmtDT(a.disparado_em)}</span><span class="fc-sub">${relTime(a.disparado_em)}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span>${a.atribuido_nome ? `<span class="fc-sub">${ico('users',11)} ${esc(a.atribuido_nome)}</span>` : ''}</td>
            <td style="text-align:right">${a.status==='ABERTO' && podeAgir
              ? `<button class="btn btn-soft btn-sm al-assumir" data-assume="${esc(a.codigo)}">${ico('users',13)} Assumir</button>`
              : `<span class="al-abrir">${ico('chev',14)}</span>`}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="8">${vazio('check','Nenhum alerta neste filtro.')}</td></tr>`}
        </tbody></table></div>
        <div class="tbl-rodape">
          <label class="por-pagina">Por página
            <select class="inp" id="alPorPagina">${[POR_PAGINA, 50, 100, 200].map(n =>
              `<option value="${n}" ${n === porPagina ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
          ${pagerHTML(r.total, r.pagina, r.por_pagina, 'alertas')}
        </div>`;

      pintarSnaps(corpo);
      $$('tr[data-a]', corpo).forEach(t => t.onclick = () => abrirAlerta(t.dataset.a));
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

  const porStatus = s => { FILTRO.alertas.status = s; PAG.alertas = 1; irPara('alerts'); };
  const aplicar = () => { PAG.alertas = 1; FILTRO.alertas = {...FILTRO.alertas, de: $('#alDe').value, ate: $('#alAte').value}; $('#alDe').max = FILTRO.alertas.ate || hoje; $('#alAte').min = FILTRO.alertas.de; $('#alLimpar').hidden = !(FILTRO.alertas.de || FILTRO.alertas.ate); carregar(); };
  $$('#alSeg button').forEach(b => b.onclick = () => porStatus(b.dataset.s));
  $$('.cm-kpi[data-st]', alvo).forEach(b => b.onclick = () => porStatus(f.status === b.dataset.st ? '' : b.dataset.st));
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
      ${a.camera && a.camera.codigo !== 'VIDEO-TESTE' && API.pode('live') ? `<button class="btn btn-soft" id="alCam">${ico('live',14)} Ver câmera</button>` : ''}
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
  EXPORTACAO:'Exportação de dados', EXPURGO:'Expurgo de capturas vencidas',
  BIO_TRIAGEM:'Triagem biométrica aprovada', VEICULO_ROTULO:'Veículo rotulado',
  VEICULO_PASSAGEM_EXCLUIDA:'Passagem de veículo excluída'
};
const rotuloAuditoria = c => ROTULO_AUDITORIA[c] || c;

/* Três estados, não dois: bloqueado volta, excluído não. */
const statusUsuario = u => u.excluido_em ? '<span class="chip c-gray">Excluído</span>'
  : u.ativo ? '<span class="chip c-green">Ativo</span>'
  : '<span class="chip c-amber">Bloqueado</span>';

const COR_PERFIL = {ADMIN:'var(--red)', SUPER:'var(--amber)', OPERADOR:'var(--acc)', ANALISTA:'var(--green)', AUDITOR:'var(--violet)'};
const CHIP_PERFIL = {ADMIN:'c-red', SUPER:'c-amber', AUDITOR:'c-violet', ANALISTA:'c-green'};

async function renderUsuarios(alvo){
  // Os setores vêm das câmeras: é o mesmo prédio, e manter uma lista à parte
  // faria a unidade do usuário divergir de onde ele de fato trabalha.
  const [perfis, setores] = await Promise.all([API.perfis(), API.setores(true)]);
  let usuarios = await API.usuarios();
  let verExcluidos = false, perfilSel = '';
  const listaPerfis = perfis.perfis || [];
  const corDe = c => COR_PERFIL[c] || 'var(--acc)';

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Usuários e perfis de acesso</h2><span class="cm-sub">contas, perfis e o que cada perfil pode fazer</span><span class="sp"></span>
    <div class="toolbar">
      <button class="btn btn-primary btn-sm" id="usNovo">${ico('plus',14)} Novo usuário</button>
    </div></div>
  <div class="dash-grid dash-kpis" id="usKpis"></div>
  <div class="cm-barra fc-barra">
    <div class="search">${ico('search',14)}<input class="inp" id="usQ" placeholder="Nome, matrícula, e-mail…"></div>
    <div class="segm" id="usPerfilSeg"></div>
    <span class="sp"></span>
    <button class="btn btn-ghost btn-sm" id="usExc">${ico('eye',14)} Ver excluídos</button>
    <span class="cm-cont" id="usCont"></span>
  </div>
  <div class="panel"><div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl fc-tbl" id="usTbl">
    <thead><tr><th>Usuário</th><th>Perfil</th><th>Setor</th><th>Verificação em 2 etapas</th>
      <th>Último acesso</th><th>Status</th><th style="width:60px"></th></tr></thead>
    <tbody></tbody></table></div></div></div>

  <div class="panel us-matriz-painel"><div class="panel-hd"><h3>Matriz de permissões</h3>
      <span class="sub">o que cada perfil acessa</span><span class="sp"></span>
      <span class="hint" style="margin:0">clique num perfil para listar os usuários dele</span></div>
    <div class="panel-bd tight"><div class="tbl-wrap" id="usMatriz"></div></div></div>`;

  const kpis = () => {
    const vivos = usuarios.filter(u => !u.excluido_em);
    const ativos = vivos.filter(u => u.ativo).length;
    const bloq = vivos.length - ativos;
    const mfa = vivos.filter(u => u.mfa_ativo).length;
    const k = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
      <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
      <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
    $('#usKpis').innerHTML = `
      ${k('', 'Usuários', 'users', fmtNum(vivos.length), `${fmtNum(ativos)} ativo(s)`, dashBarra(vivos.length ? ativos / vivos.length : 0, 'var(--acc)'))}
      ${k('k-amber', 'Bloqueados', 'lock', fmtNum(bloq), bloq ? 'sem acesso até desbloquear' : 'nenhuma conta bloqueada', dashBarra(vivos.length ? bloq / vivos.length : 0, 'var(--amber)'))}
      ${k('k-green', 'Verificação em 2 etapas', 'shield', pctCurto(mfa, vivos.length), `${fmtNum(mfa)} de ${fmtNum(vivos.length)} conta(s)`, dashBarra(vivos.length ? mfa / vivos.length : 0, 'var(--green)'))}
      ${k('k-violet', 'Perfis de acesso', 'key', fmtNum(listaPerfis.length), `${Object.keys(PERMS_ROTULO).length} permissões possíveis`,
        `<div class="cm-cob">${listaPerfis.map(p => `<i style="flex:1;max-width:none;background:${corDe(p.codigo)}"></i>`).join('')}</div>`)}`;
    const qtd = c => vivos.filter(u => u.perfil === c).length;
    $('#usPerfilSeg').innerHTML = [['', 'Todos', vivos.length], ...listaPerfis.map(p => [p.codigo, rotuloPerfil(p.codigo), qtd(p.codigo)])]
      .map(([c, l, q]) => `<button data-p="${c}" class="${perfilSel === c ? 'on' : ''}">${esc(l)} <em>${fmtNum(q)}</em></button>`).join('');
    $$('#usPerfilSeg button').forEach(b => b.onclick = () => porPerfil(b.dataset.p));
    matriz(qtd);
  };

  // Linhas = permissões, colunas = perfis: dá para comparar perfis num olhar,
  // o que os cartões lado a lado não deixavam.
  const matriz = qtd => {
    const perms = Object.keys(PERMS_ROTULO);
    $('#usMatriz').innerHTML = `<table class="tbl us-matriz">
      <thead><tr><th>Permissão</th>${listaPerfis.map(p => `<th>
        <button type="button" class="us-perfil ${perfilSel === p.codigo ? 'on' : ''}" data-mp="${esc(p.codigo)}" style="--pf:${corDe(p.codigo)}">
          <i></i><b>${esc(rotuloPerfil(p.codigo))}</b><span class="mono">${fmtNum(qtd(p.codigo))} usuário(s) · ${p.permissoes.length}/${perms.length}</span></button></th>`).join('')}</tr></thead>
      <tbody>${perms.map(k => `<tr><td>${esc(PERMS_ROTULO[k])}</td>${listaPerfis.map(p => `<td class="${perfilSel === p.codigo ? 'on' : ''}">${p.permissoes.includes(k)
          ? `<span class="us-sim" style="--pf:${corDe(p.codigo)}">${ico('check', 13)}</span>`
          : '<span class="us-nao">—</span>'}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    $$('[data-mp]').forEach(b => b.onclick = () => porPerfil(perfilSel === b.dataset.mp ? '' : b.dataset.mp));
  };

  const pintar = lista => {
    $('#usCont').textContent = `${fmtNum(lista.length)} usuário(s)`;
    $('#usTbl tbody').innerHTML = lista.map(u => `
      <tr class="user-row ${u.excluido_em ? '' : 'clickable'}" ${u.excluido_em ? '' : `data-u="${esc(u.id)}"`}
          style="${u.excluido_em ? 'opacity:.55' : ''}">
        <td><div class="fc-pessoa"><span class="avatar us-av" style="--pf:${corDe(u.perfil)}">${esc(iniciais(u.nome))}</span>
          <div><b>${esc(u.nome)}</b><span class="mono">${esc(u.email)}${u.matricula ? ` · ${esc(u.matricula)}` : ''}</span></div></div></td>
        <td><span class="chip ${CHIP_PERFIL[u.perfil] || 'c-blue'}">${esc(rotuloPerfil(u.perfil))}</span></td>
        <td><span class="fc-cam">${esc(u.unidade || '—')}</span></td>
        <td>${u.mfa_ativo
          ? `<span class="ob-sev" style="--sev:var(--green)"><i></i>Ativa</span>`
          : `<span class="ob-sev" style="--sev:var(--amber)"><i></i>Desativada</span>`}</td>
        <td>${u.ultimo_acesso ? `<span class="fc-hora mono">${fmtDT(u.ultimo_acesso)}</span><span class="fc-sub">${relTime(u.ultimo_acesso)}</span>`
          : '<span class="fc-sub">nunca acessou</span>'}</td>
        <td>${statusUsuario(u)}</td>
        <td style="text-align:right">${u.excluido_em ? ''
          : `<button class="btn btn-ghost btn-sm btn-icon" data-eu="${esc(u.id)}" title="Editar">${ico('edit',14)}</button>`}</td>
      </tr>`).join('') || `<tr><td colspan="7" style="text-align:center;padding:24px">${vazio('users','Nenhum usuário encontrado.')}</td></tr>`;
    $$('#usTbl [data-eu]').forEach(b => b.onclick = e => { e.stopPropagation(); editarUsuario(b.dataset.eu, usuarios, perfis, setores); });
    $$('#usTbl tr[data-u]').forEach(t => t.onclick = () => editarUsuario(t.dataset.u, usuarios, perfis, setores));
  };

  const filtrados = () => {
    const q = $('#usQ').value.toLowerCase().trim();
    return usuarios.filter(u => (!perfilSel || u.perfil === perfilSel) && (!q ||
      [u.nome, u.email, u.matricula, u.unidade].join(' ').toLowerCase().includes(q)));
  };
  const porPerfil = c => { perfilSel = c; kpis(); pintar(filtrados()); };

  $('#usQ').oninput = debounce(() => pintar(filtrados()), 200);
  $('#usNovo').onclick = () => editarUsuario(null, usuarios, perfis, setores);
  $('#usExc').onclick = async () => {
    verExcluidos = !verExcluidos;
    // Contas excluídas seguem ocupando e-mail e matrícula; sem poder vê-las,
    // o cadastro de um substituto homônimo falharia sem explicação.
    $('#usExc').innerHTML = ico('eye',14) + (verExcluidos ? ' Ocultar excluídos' : ' Ver excluídos');
    $('#usExc').classList.toggle('on', verExcluidos);
    usuarios = await API.usuarios(null, verExcluidos);
    kpis(); pintar(filtrados());
  };
  kpis();
  pintar(filtrados());
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
/* Cada ação da trilha cai numa categoria: dá a cor, o ícone e o grupo do filtro. */
const CATEGORIAS_AUDITORIA = [
  {k: 'acesso',   nome: 'Acesso',      ico: 'key',    cor: 'var(--acc)',    teste: a => /^(LOGIN|LOGOUT|SENHA_)/.test(a)},
  {k: 'bio',      nome: 'Biometria',   ico: 'face',   cor: 'var(--violet)', teste: a => /^(BIO_|CONSULTA_BIO|PESSOA_)/.test(a)},
  {k: 'deteccao', nome: 'Detecções',   ico: 'box',    cor: 'var(--green)',  teste: a => /^(DET_|VEICULO_)/.test(a)},
  {k: 'alerta',   nome: 'Alertas',     ico: 'bell',   cor: 'var(--amber)',  teste: a => /^ALERTA_/.test(a)},
  {k: 'camera',   nome: 'Câmeras',     ico: 'cam',    cor: '#4FC3C9',       teste: a => /^(CAM_|PTZ)/.test(a)},
  {k: 'usuario',  nome: 'Usuários',    ico: 'users',  cor: '#E05F9E',       teste: a => /^USR_/.test(a)},
  {k: 'sistema',  nome: 'Sistema',     ico: 'cog',    cor: 'var(--txt-3)',  teste: () => true},
];
const categoriaAuditoria = a => CATEGORIAS_AUDITORIA.find(c => c.teste(a || ''));
/* Ações que a corregedoria costuma procurar primeiro: saem marcadas na lista. */
const auSensivel = a => /EXCLUSAO|_DEL$|EXPORTACAO|EXPURGO|CONFIG_EDIT|LIMIAR_EDIT|LOGIN_BLOQUEADO|BIO_FUSAO|BIO_RENUMERA/.test(a || '');

async function renderAuditoria(alvo){
  const acoes = await API.acoesAuditoria();
  const tot = acoes.reduce((s, a) => s + (a.total || 0), 0);
  const somaDe = re => acoes.filter(a => re.test(a.acao)).reduce((s, a) => s + (a.total || 0), 0);
  const consultas = somaDe(/^CONSULTA_BIO$/);
  const sensiveis = acoes.filter(a => auSensivel(a.acao)).reduce((s, a) => s + (a.total || 0), 0);
  const k = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
    <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
    <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Trilha de auditoria</h2><span class="cm-sub">registro imutável de operações</span><span class="sp"></span>
    <div class="toolbar">
      <button class="btn btn-ghost btn-sm" id="auVerificar">${ico('shield',14)} Verificar cadeia</button>
      ${API.pode('export') ? `<button class="btn btn-ghost btn-sm" id="auExp">${ico('dl',14)} Exportar PDF</button>` : ''}
    </div></div>

  <div class="dash-grid dash-kpis">
    ${k('', 'Registros', 'log', fmtNum(tot), `${acoes.length} tipo(s) de ação`,
      `<div class="cm-cob">${CATEGORIAS_AUDITORIA.map(c => {
        const v = acoes.filter(a => categoriaAuditoria(a.acao) === c).reduce((s, a) => s + a.total, 0);
        return v ? `<i title="${esc(c.nome)}: ${fmtNum(v)}" style="width:${(v / tot * 100).toFixed(1)}%;max-width:none;background:${c.cor}"></i>` : '';
      }).join('')}</div>`)}
    ${k('k-violet', 'Consultas biométricas', 'face', fmtNum(consultas), `${pctCurto(consultas, tot)} da trilha · vinculadas à matrícula`, dashBarra(tot ? consultas / tot : 0, 'var(--violet)'))}
    ${k('k-amber', 'Operações sensíveis', 'alert', fmtNum(sensiveis), 'exclusões, exportações e ajustes', dashBarra(tot ? sensiveis / tot : 0, 'var(--amber)'))}
    <div class="kpi k-green au-cadeia" id="auCadeia">
      <div class="kpi-top"><span class="kpi-lbl">Integridade da cadeia</span><span class="kpi-ico">${ico('shield',15)}</span></div>
      <div class="kpi-val au-cadeia-val">Não verificada</div>
      <div class="kpi-delta nt au-cadeia-sub">use “Verificar cadeia”, no alto da tela</div>
    </div>
  </div>

  <div class="cm-barra fc-barra">
    <div class="search">${ico('search',14)}<input class="inp" id="auQ" value="${esc(FILTRO.audit.q)}" placeholder="Usuário, ação, alvo, IP…"></div>
    <select class="inp" id="auAcao"><option value="">Todas as ações</option>
      ${CATEGORIAS_AUDITORIA.map(c => {
        const doGrupo = acoes.filter(a => categoriaAuditoria(a.acao) === c);
        return doGrupo.length ? `<optgroup label="${esc(c.nome)}">${doGrupo.map(a =>
          `<option value="${esc(a.acao)}" ${FILTRO.audit.acao===a.acao?'selected':''}>${esc(rotuloAuditoria(a.acao))} (${fmtNum(a.total)})</option>`).join('')}</optgroup>` : '';
      }).join('')}</select>
    <span class="sp"></span>
    <span class="cm-cont" id="auCont"></span>
  </div>
  <p class="fc-nota" title="Uma trigger no banco recusa UPDATE e DELETE. Consulta de biométricos, exportação ou mudança de limiar fica vinculada à matrícula do operador.">${ico('lock',13)}<span><b>Somente inserção:</b> cada registro é encadeado por hash ao anterior e não pode ser alterado nem apagado.</span></p>
  <div id="auVerif"></div>
  <div class="panel"><div class="panel-bd tight"><div id="auCorpo">${carregando()}</div></div></div>`;

  /* Quantos registros o filtro atual alcança. Guardado da última consulta
     para o diálogo de exportação poder dizer o tamanho do que vai sair. */
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
      const cont = $('#auCont'); if(cont) cont.textContent = `${fmtNum(r.total || 0)} registro(s)`;
      const itens = r.itens || [];
      const local = ip => ip === '::1' || ip === '127.0.0.1';

      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl fc-tbl au-tbl">
        <thead><tr><th>Quando</th><th>Ação</th><th>Descrição</th><th>Usuário</th><th>Alvo</th><th>Origem</th></tr></thead>
        <tbody>${itens.map(a => {
          const c = categoriaAuditoria(a.acao);
          return `<tr style="--cat:${c.cor}">
          <td><span class="fc-hora mono">${fmtDT(a.ocorrido_em)}</span><span class="fc-sub">${relTime(a.ocorrido_em)}</span></td>
          <td><div class="fc-pessoa"><span class="ob-ico" style="color:${c.cor};background:color-mix(in srgb, ${c.cor} 15%, transparent)">${ico(c.ico, 14)}</span>
            <div><b>${esc(rotuloAuditoria(a.acao))}${auSensivel(a.acao) ? ' <em class="au-sens">sensível</em>' : ''}</b>
              <span class="mono">${esc(c.nome)} · ${esc(a.acao)}</span></div></div></td>
          <td class="au-desc"><span title="${esc(a.descricao || '')}">${esc(a.descricao || '—')}</span></td>
          <td>${a.usuario_nome ? `<div class="fc-pessoa"><span class="avatar au-av">${esc(iniciais(a.usuario_nome))}</span>
            <div><b>${esc(a.usuario_nome)}</b><span class="mono">${esc((a.usuario_email||'').split('@')[0]||'')}</span></div></div>`
            : '<span class="fc-sub">sistema</span>'}</td>
          <td>${a.alvo ? `<span class="au-alvo mono" title="${esc(a.alvo)}">${esc(a.alvo)}</span>` : '<span class="fc-sub">—</span>'}</td>
          <td><span class="fc-hora mono">${esc(a.ip || '—')}</span>${a.ip ? `<span class="fc-sub">${local(a.ip) ? 'esta máquina' : 'rede'}</span>` : ''}</td>
        </tr>`;
        }).join('') || `<tr><td colspan="6" style="text-align:center;padding:24px">${vazio('log','Nenhum registro.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'audit')}`;
      ligarPager(corpo, 'audit', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#auQ').oninput = debounce(() => { PAG.audit = 1; carregar(); }, 350);
  $('#auAcao').onchange = () => { PAG.audit = 1; carregar(); };

  $('#auVerificar').onclick = async () => {
    const box = $('#auVerif'), card = $('#auCadeia'), btn = $('#auVerificar');
    btn.disabled = true;
    $('.au-cadeia-val', card).textContent = '…';
    $('.au-cadeia-sub', card).textContent = 'recalculando a cadeia…';
    box.innerHTML = '';
    try{
      const v = await API.verificarCadeia();
      /* Três veredictos, não dois. "Comprometida" em vermelho é para linha
         reescrita — e só. Elos bifurcados por gravação simultânea e vínculos
         zerados pela exclusão de uma conta não são adulteração; pintar tudo
         de vermelho ensina o operador a ignorar o aviso. */
      const adulteradas = (v.conteudo_divergente||0) - (v.vinculo_de_usuario_perdido||0);
      const nivel = v.integra ? 'ok' : (adulteradas > 0 ? 'err' : 'warn');
      const titulo = v.integra ? 'Íntegra' : (adulteradas > 0 ? 'Comprometida' : 'Com ressalvas');
      card.classList.remove('k-green', 'k-amber', 'k-red');
      card.classList.add(nivel === 'ok' ? 'k-green' : nivel === 'warn' ? 'k-amber' : 'k-red');
      $('.au-cadeia-val', card).textContent = titulo;
      $('.au-cadeia-sub', card).textContent = `verificada ${fmtDT(new Date().toISOString())}`;
      if(nivel !== 'ok') box.innerHTML = `<div class="alert alert-${nivel}" style="margin-bottom:13px">
        ${ico('alert', 15)}<div><b>Cadeia ${titulo.toLowerCase()}</b><br>${esc(v.mensagem)}</div></div>`;
      else card.title = v.mensagem || '';
    }catch(e){
      $('.au-cadeia-val', card).textContent = '—';
      $('.au-cadeia-sub', card).textContent = 'não foi possível verificar';
      box.innerHTML = `<div class="alert alert-err" style="margin-bottom:13px">${ico('alert',15)}<div>${esc(e.message)}</div></div>`;
    }finally{ btn.disabled = false; }
  };

  const be = $('#auExp');
  if(be) be.onclick = async () => {
    /* O que vai no arquivo é o que está na tela — dito *antes*, para ninguém
       entregar à corregedoria um recorte achando que levou tudo. */
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

    // O botão sai do ar enquanto o documento é montado: dois cliques gerariam
    // dois documentos e duas linhas na própria trilha.
    const antes = be.innerHTML;
    be.disabled = true;
    be.innerHTML = `${ico('dl',14)} Gerando o PDF…`;
    try{
      const nome = await API.exportarAuditoria(fin, filtro);
      toast('Trilha exportada', `Documento ${nome} salvo nos downloads.`, 'ok');
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
  ['retencao', 'Retenção e privacidade', 'shield', 'quanto tempo a evidência fica e quem vê o dado pessoal'],
  ['acesso',   'Acesso e sessão',        'key',    'senha, bloqueio e duração da sessão'],
  ['deteccao', 'Detecção',               'gauge',  'sensibilidade dos motores de análise'],
];

/* Um parâmetro por linha: o que é à esquerda, o valor à direita. O `min`/`max`
   aqui é conveniência de digitação — quem recusa de verdade é o servidor,
   porque o navegador não é lugar para guardar regra. */
function campoCfg(p){
  const id = 'cfg_' + p.chave;
  const texto = `<div class="cf-txt"><label for="${id}">${esc(p.rotulo)}</label><span class="hint">${esc(p.ajuda)}</span></div>`;
  if(p.tipo === 'perfis'){
    const atuais = String(p.valor || '').split(',').map(x => x.trim()).filter(Boolean);
    return `<div class="cf-linha" data-cfg="${esc(p.chave)}" data-tipo="perfis">${texto}
      <div class="cf-val"><div class="cf-perfis">
        ${(p.opcoes||[]).map(o => `<button type="button" class="cf-perfil ${atuais.includes(o)?'on':''}" data-perfil="${esc(o)}"
          aria-pressed="${atuais.includes(o)}">${ico('check',12)}${esc(rotuloPerfil(o))}</button>`).join('')}
      </div></div></div>`;
  }
  const passo = p.tipo === 'float' ? '0.01' : '1';
  return `<div class="cf-linha" data-cfg="${esc(p.chave)}" data-tipo="${esc(p.tipo)}">${texto}
    <div class="cf-val">
      <div class="cf-inp"><input class="inp mono" id="${id}" type="number" value="${esc(String(p.valor))}"
           min="${p.minimo}" max="${p.maximo}" step="${passo}">${p.unidade ? `<span>${esc(p.unidade)}</span>` : ''}</div>
      <span class="cf-faixa mono">faixa ${p.minimo} – ${p.maximo}</span>
    </div></div>`;
}

async function renderConfiguracoes(alvo){
  const [info, saude, cfg] = await Promise.all([API.info(), API.saude(), API.configuracao()]);
  const c = info.config || {}, est = info.estatisticas || {}, mod = info.modelos || {};
  const params = cfg.parametros || [];
  const pode = API.pode('settings');
  const k = (cls, lbl, icone, val, sub, extra) => `<div class="kpi ${cls}">
    <div class="kpi-top"><span class="kpi-lbl">${lbl}</span><span class="kpi-ico">${ico(icone,15)}</span></div>
    <div class="kpi-val">${val}</div>${extra || ''}<div class="kpi-delta nt">${sub}</div></div>`;
  const motores = [mod.face, mod.yolo].filter(Boolean).length;
  const linha = (rot, val) => `<div class="cf-kv"><span>${rot}</span><b>${val}</b></div>`;

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Configurações do sistema</h2><span class="cm-sub">parâmetros do sistema e da análise</span><span class="sp"></span>
    <div class="toolbar">
      ${pode ? `<button class="btn btn-ghost btn-sm" id="cfgExpurgo">${ico('trash',13)} Expurgar capturas vencidas</button>` : ''}
    </div></div>

  <div class="dash-grid dash-kpis">
    ${k(saude.saudavel ? 'k-green' : 'k-red', 'Infraestrutura', 'gauge', saude.saudavel ? 'Saudável' : 'Degradado',
        `banco ${esc(saude.componentes.banco || '—')} · ${fmtNum(saude.websockets_ativos || 0)} conexão(ões) ao vivo`)}
    ${k(motores === 2 ? '' : 'k-amber', 'Motores de análise', 'eye', `${motores}<small>/2</small>`,
        `${mod.face ? 'InsightFace' : 'facial ausente'} · ${mod.yolo ? 'YOLO' : 'objetos ausente'} · ${esc(c.device || 'cpu')}`,
        dashBarra(motores / 2, motores === 2 ? 'var(--acc)' : 'var(--amber)'))}
    ${k('k-violet', 'Limiares globais', 'filter', `${Math.round((c.limiar_facial||0)*100)}<small>% facial</small>`,
        `objetos ${Math.round((c.limiar_objeto||0)*100)}% · vetor ${c.embedding_dim || 512}-D · ajuste fino por câmera`,
        dashBarra(c.limiar_facial || 0, 'var(--violet)'))}
    ${k('k-amber', 'Retenção', 'clock', `${c.retencao_dias || 90}<small> dias</small>`,
        'para capturas sem identidade', dashBarra(Math.min(1, (c.retencao_dias || 90) / 365), 'var(--amber)'))}
  </div>

  <div class="cf-layout">
    <div class="cf-main">
      <div class="panel">
        <div class="panel-hd"><h3>Parâmetros ajustáveis</h3>
          <span class="sub">valem na requisição seguinte, sem reiniciar</span><span class="sp"></span>
          <span class="cf-pend" id="cfgPend" hidden></span>
          ${pode ? `<button class="btn btn-ghost btn-sm" id="cfgDesfazer" hidden>Desfazer</button>
          <button class="btn btn-primary btn-sm" id="cfgSalvar" disabled>${ico('check',14)} Salvar alterações</button>` : ''}</div>
        <div class="panel-bd cf-grupos">
          ${GRUPOS_CFG.map(([g, titulo, icone, desc]) => {
            const doGrupo = params.filter(p => p.grupo === g);
            if(!doGrupo.length) return '';
            return `<section class="cf-grupo">
              <header><span class="cf-gico">${ico(icone,15)}</span><div><b>${esc(titulo)}</b><span>${esc(desc)}</span></div></header>
              ${doGrupo.map(campoCfg).join('')}</section>`;
          }).join('')}
        </div>
      </div>

      <div class="panel">
        <div class="panel-hd"><h3>Parâmetros fixos</h3><span class="sub">lidos quando o servidor sobe</span><span class="sp"></span>
          <span class="chip c-gray">${ico('lock',11)} exigem reinício</span></div>
        <div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl cf-fixos">
          <thead><tr><th>Parâmetro</th><th>Valor em uso</th><th>Variável no .env</th></tr></thead>
          <tbody>${(cfg.fixos||[]).map(f => `<tr><td>${esc(f.rotulo)}</td>
            <td><b class="mono">${esc(String(f.valor))}</b></td><td><span class="au-alvo mono">${esc(f.variavel)}</span></td></tr>`).join('')
            || `<tr><td colspan="3">${vazio('lock', 'Nenhum parâmetro fixo.')}</td></tr>`}</tbody></table></div>
          <p class="fc-nota cf-nota">${ico('info',13)}<span>Mudá-los é no <span class="mono">.env</span> seguido de reinício — gravá-los aqui mostraria um número que o sistema não está usando. Toda alteração dos ajustáveis vai para a trilha de auditoria com o valor anterior e o novo.</span></p>
        </div>
      </div>
    </div>

    <aside class="cf-lado">
      <div class="panel"><div class="panel-hd"><h3>Motores de análise</h3></div><div class="panel-bd">
        ${linha('Reconhecimento facial', mod.face ? '<span class="ob-sev" style="--sev:var(--green)"><i></i>InsightFace</span>' : '<span class="ob-sev" style="--sev:var(--amber)"><i></i>não instalado</span>')}
        ${linha('Detecção de objetos', mod.yolo ? '<span class="ob-sev" style="--sev:var(--green)"><i></i>YOLO</span>' : '<span class="ob-sev" style="--sev:var(--amber)"><i></i>não instalado</span>')}
        ${linha('Limiar global facial', `<span class="mono">${Math.round((c.limiar_facial||0)*100)}%</span>`)}
        ${linha('Limiar global objeto', `<span class="mono">${Math.round((c.limiar_objeto||0)*100)}%</span>`)}
        ${linha('Dimensão do vetor', `<span class="mono">${c.embedding_dim || 512}-D</span>`)}
        ${linha('Dispositivo', `<span class="mono">${esc(c.device||'cpu')}</span>`)}
        <p class="fc-nota cf-nota" title="Limiares baixos aumentam falsos positivos — e o custo de um falso positivo recai sobre uma pessoa inocente.">${ico('alert',13)}<span>O limiar fino é ajustado <b>por câmera</b>, na ficha de cada uma.</span></p>
      </div></div>

      <div class="panel"><div class="panel-hd"><h3>Infraestrutura</h3><span class="sp"></span>
          <span class="chip ${saude.saudavel?'c-green':'c-red'}">${saude.saudavel?'Saudável':'Degradado'}</span></div><div class="panel-bd">
        ${linha('Versão', `<span class="mono">AizonVMS ${esc(info.versao||'')}</span>`)}
        ${linha('Modo', `<span class="mono">${esc(info.modo||'—')}</span>`)}
        ${linha('Banco', `<span class="mono">${esc(saude.componentes.banco || '—')}</span>`)}
        ${linha('Busca vetorial', `<span class="mono">${esc(info.busca_vetorial||'—')}</span>`)}
        ${linha('Barramento', `<span class="mono">${esc(saude.componentes.eventos||'—')}</span>`)}
        ${linha('Conexões ao vivo', `<span class="mono">${fmtNum(saude.websockets_ativos || 0)}</span>`)}
        ${info.simulador ? linha('Simulador', `<span class="chip c-blue">Ativo · ${fmtNum(info.simulador_gerados||0)}</span>`) : ''}
      </div></div>

      <div class="panel"><div class="panel-hd"><h3>Dados armazenados</h3></div><div class="panel-bd cf-est">
        ${[['cam','Câmeras', est.cameras], ['db','Registros biométricos', est.pessoas], ['box','Detecções', est.deteccoes],
           ['bell','Alertas', est.alertas], ['log','Linhas de auditoria', est.auditoria], ['users','Usuários', est.usuarios]]
          .map(([i, r, v]) => `<div><span class="cf-gico">${ico(i,13)}</span><em>${r}</em><b class="mono">${fmtNum(v || 0)}</b></div>`).join('')}
      </div></div>

      <div class="panel cf-lgpd"><div class="panel-bd">
        <div class="cf-lgpd-hd">${ico('shield',15)}<b>LGPD · dado biométrico</b></div>
        <p>Exige <b>base legal específica</b>, aviso às pessoas monitoradas e avaliação de impacto. Em ambiente de trabalho, depende ainda da norma interna e da legislação trabalhista.</p>
        ${linha('Mascaramento de dado pessoal', esc((c.mascarar_para||[]).map(rotuloPerfil).join(', ') || 'nenhum perfil'))}
      </div></div>
    </aside>
  </div>`;

  /* O que mudou em relação ao valor gravado. Serve ao botão Salvar e ao
     contador — salvar sem mudança nenhuma não tem por que ser possível. */
  const coletar = () => {
    const valores = {};
    params.forEach(p => {
      const campo = $(`[data-cfg="${p.chave}"]`);
      if(!campo) return;
      let mudou = false;
      if(p.tipo === 'perfis'){
        const escolhidos = $$('[data-perfil]', campo).filter(x => x.classList.contains('on')).map(x => x.dataset.perfil);
        const atual = String(p.valor || '').split(',').map(x => x.trim()).filter(Boolean);
        if(escolhidos.join(',') !== atual.join(',')){ valores[p.chave] = escolhidos.join(','); mudou = true; }
      }else{
        const bruto = $('input', campo).value.trim();
        if(bruto !== '' && Number(bruto) !== Number(p.valor)){ valores[p.chave] = Number(bruto); mudou = true; }
      }
      campo.classList.toggle('mudou', mudou);
    });
    return valores;
  };
  const atualizar = () => {
    const n = Object.keys(coletar()).length;
    const s = $('#cfgSalvar'); if(s) s.disabled = !n;
    const d = $('#cfgDesfazer'); if(d) d.hidden = !n;
    const pend = $('#cfgPend'); pend.hidden = !n;
    pend.textContent = `${n} alteração(ões) não salva(s)`;
  };

  $$('[data-cfg][data-tipo="perfis"] [data-perfil]').forEach(ch => ch.onclick = () => {
    if(!pode) return;
    const on = ch.classList.toggle('on');
    ch.setAttribute('aria-pressed', String(on));
    atualizar();
  });
  $$('.cf-linha input').forEach(i => { i.disabled = !pode; i.oninput = atualizar; });

  const desfazer = $('#cfgDesfazer');
  if(desfazer) desfazer.onclick = () => irPara('settings');

  const salvar = $('#cfgSalvar');
  if(salvar) salvar.onclick = async () => {
    const valores = coletar();
    if(!Object.keys(valores).length){ toast('Nada a salvar', 'Nenhum parâmetro foi alterado.', 'info'); return; }
    salvar.disabled = true;
    try{
      const r = await API.salvarConfiguracao(valores);
      toast('Configuração salva', `${r.alterados.length} parâmetro(s) em vigor a partir de agora.`, 'ok');
      irPara('settings');
    }catch(e){ erroToast(e); salvar.disabled = false; }
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
