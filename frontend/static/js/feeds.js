/* ==========================================================
   SEGURANÇA — Motor de vídeo sintético (frontend)

   Renderiza cenas procedurais em <canvas> para dar imagem às
   câmeras enquanto não há RTSP real ligado. A troca por vídeo
   de verdade é local: no mosaico, substitua o <canvas> por um
   <video> apontado à URL WebRTC de GET /stream/{codigo}.

   Os dados (câmeras, detecções, pessoas) vêm todos da API —
   este arquivo só desenha.
   ========================================================== */
'use strict';

/* ---------- Utilidades ---------- */
function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function hash32(s){let h=2166136261;for(let i=0;i<String(s).length;i++){h^=String(s).charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
const rnd=()=>Math.random();
const rf=(a,b)=>Math.random()*(b-a)+a;
const ri=(a,b)=>Math.floor(Math.random()*(b-a+1))+a;
const pick=arr=>arr[Math.floor(Math.random()*arr.length)];

/* As chaves ficam como estão — são o que está gravado em cada câmera. Só os
   rótulos mudaram, de cenário de rua para o que existe neste prédio. */
const CENAS={
  rua:       {lbl:'Área interna',     ceu:'#101C28', chao:'#15202B'},
  avenida:   {lbl:'Hall',             ceu:'#0E1A26', chao:'#131E29'},
  praca:     {lbl:'Sala / Reunião',   ceu:'#0F1D22', chao:'#16232A'},
  portaria:  {lbl:'Portaria',         ceu:'#141A22', chao:'#1A222B'},
  corredor:  {lbl:'Corredor',         ceu:'#161D25', chao:'#1C242D'},
  estac:     {lbl:'Garagem',          ceu:'#0D141C', chao:'#141C25'},
  perimetro: {lbl:'Área externa',     ceu:'#0B1219', chao:'#111A22'},
  terminal:  {lbl:'Recepção',         ceu:'#141C26', chao:'#1A232D'}
};

const SEXOS={
  M:{lbl:'Masculino',     pfx:'BIO-M', cor:'#35A7DE'},
  F:{lbl:'Feminino',      pfx:'BIO-F', cor:'#E05F9E'},
  N:{lbl:'Não informado', pfx:'BIO-N', cor:'#5F7589'}
};

/* Pessoa em verde e rosto em rosa — o padrão do visualizador de testes, onde
   a usuária validou a leitura. Veículo passou a azul para não disputar o
   verde com a pessoa; mochila saiu do rosa pelo mesmo motivo. */
const COR_CLASSE={
  pessoa:'#2FC27A', carro:'#35A7DE', caminhao:'#E8712C', onibus:'#D9C13A',
  motocicleta:'#F0A02A', bicicleta:'#8C6FE0', animal:'#4ED1C5',
  mochila:'#C79A5B', volume:'#E8474D', capacete:'#5FC4F0', placa:'#D9C13A', arma:'#E8474D',
  veiculo:'#35A7DE',   // vocabulário da cena sintética, que não distingue o tipo
  rosto:'#E05F9E',     // rosto e corpo saem de modelos diferentes sobre a mesma
                       // pessoa: cores distintas deixam ver as duas caixas
  FACIAL:'#E05F9E'
};
/* Só vale enquanto o worker não disser quanto dura a volta dele pelo parque:
   quem sabe disso é ele, e o número cresce a cada câmera cadastrada. Serve de
   piso para o primeiro quadro e para quando a mensagem vier sem a validade. */
const CAIXA_VIDA_MS=5000;
/* Em quanto tempo a caixa percorre a distância entre a posição publicada
   antes e a de agora.

   O worker entrega uma análise por câmera a cada segundo e pouco, e o vídeo
   roda a seis quadros por segundo: a caixa desenhada direto na posição nova
   dá um salto visível a cada publicação, e quem olha vê a marcação pular em
   vez de acompanhar a pessoa.

   Curto de propósito, e menor que o intervalo entre duas publicações: a
   caixa chega ao lugar certo antes da próxima notícia, então ela nunca
   mostra uma posição que o worker já desmentiu. Esticar isto até o intervalo
   inteiro deixaria a caixa permanentemente atrás da pessoa. */
const TRANSICAO_MS=320;
/* Quantos tiles podem manter fluxo MJPEG contínuo. O navegador permite seis
   conexões HTTP por origem, e o MJPEG não devolve a sua enquanto estiver
   tocando. Com dezoito câmeras, cinco em fluxo dá espaço para o WebSocket,
   o rodízio, e as chamadas da API. Acima deste número os tiles passam a
   recarregar quadro a quadro. */
const MAX_MJPEG=5;
/* Quanto esperar antes de reinsistir num fluxo que falhou, e o teto desse
   espaçamento. A negociação RTSP de uma câmera lenta leva minutos, e marteá-la
   a cada segundo só gastaria conexão — mas desistir de vez deixa o tile preto
   depois que ela volta, que é o erro que estes números existem para evitar. */
const RETOMAR_MS=1500;
const RETOMAR_MAX_MS=20000;
/* Intervalo entre um pedido de recarga e o próximo. Não é a taxa de cada
   tile: os pedidos são feitos um de cada vez, em rodízio, então cada câmera
   volta a cada POLL_MS vezes o número de tiles em recarga. Com dezoito
   câmeras e cinco em fluxo contínuo, treze em rodízio levaria ~1,3s por volta
   a 100ms. */
const POLL_MS=100;


/* ---------- Overlays sobre a imagem da câmera ---------- */
/* A zona é desenhada por cima do que a câmera vê, e o que ela vê muda: a
   mesma linha cai sobre grama ao sol, parede branca e asfalto. Cor sozinha
   não resolve — sobre a grama da Garagem a linha clara sumia.

   Por isso o traço vai duas vezes: um vulto escuro por baixo, mais largo, e
   a cor da zona por cima. O contorno não depende do fundo nem da cor
   escolhida, e é o que faz a linha existir sobre claro.

   A espessura acompanha o tile, como já acontece com as caixas do worker.
   Antes era fixa em 1,3px: no mosaico 4×4 ficava grossa e no 1×1, onde a
   célula é a tela inteira, virava fio de cabelo. */
function drawZonas(ctx,cam,W,H,k){
  // Mesma medida em pixel de tela das caixas: no 2x2 o canvas também é
  // maior que o tile, e a linha chegava reduzida à tela.
  const s=k||1;
  (cam.zonas||[]).forEach(z=>{
    const pts=z.pontos||[];
    if(pts.length<2)return;
    ctx.save();
    ctx.lineJoin='round';ctx.lineCap='round';
    ctx.beginPath();
    pts.forEach((p,i)=>{const x=p[0]*W,y=p[1]*H;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});
    if(z.tipo==='POLIGONO'){ctx.closePath();ctx.fillStyle=z.cor+'1F';ctx.fill()}
    ctx.setLineDash(z.tipo==='LINHA'?[]:[6*s,4*s]);
    ctx.strokeStyle='rgba(0,0,0,0.60)';
    ctx.lineWidth=Math.max(3.4,4.6*s);
    ctx.stroke();
    ctx.strokeStyle=z.cor;
    ctx.lineWidth=Math.max(1.7,2.3*s);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  });
}
/* Caixas vindas do worker pelo WebSocket — cor por classe. São o que o
   modelo realmente detectou no quadro da câmera: não há mais nenhuma outra
   caixa desenhada sobre o vídeo. */
function pintarCaixasReais(ctx,lista,W,H,k){
  if(!lista.length)return;
  // `k` converte pixel de tela em pixel de canvas. Medir em pixel de tela é
  // o que faz a caixa ter a mesma presença no 4x4 e no 1x1 — antes ela era
  // proporcional ao canvas, que no mosaico é o dobro do que se vê.
  const e=k||1;
  ctx.save();
  ctx.lineWidth=2.4*e;
  /* Toda caixa leva a tarja com o que é e a confiança ("PESSOA 85%"), como no
     visualizador de testes — sem ela o operador via um retângulo e não sabia
     o que o sistema tinha concluído. Em tile pequeno a letra encolhe; quando
     duas tarjas se atropelam, a segunda entra no canto de dentro da própria
     caixa em vez de sumir. */
  const larguraTela=W/e;
  const tileGrande=larguraTela>=560;
  const fonte=tileGrande?11:9;
  ctx.font=`600 ${fonte*e}px "IBM Plex Sans",system-ui,sans-serif`;
  ctx.textBaseline='middle';
  const ocupados=[];
  const colide=(x,y,w,h)=>ocupados.some(o=>x<o.x+o.w&&x+w>o.x&&y<o.y+o.h&&y+h>o.y);
  // Os reconhecidos primeiro: são os que nunca podem perder a tarja.
  lista=[...lista].sort((a,b)=>(b.pessoa_nome?1:0)-(a.pessoa_nome?1:0));
  lista.forEach(c=>{
    const b=c.bbox;
    if(!b||b.length<4)return;
    // bbox normalizado é [x, y, largura, altura] — não [x1, y1, x2, y2].
    const x=b[0]*W,y=b[1]*H,w=b[2]*W,h=b[3]*H;
    // Rosto reconhecido ganha o nome e a cor de destaque: a caixa precisa
    // dizer *quem* é, não que há alguém ali. Vive o mesmo tempo da caixa,
    // que é a validade publicada pelo worker — a identidade não pode sumir
    // antes de a câmera ser revisitada.
    const ident=!!c.pessoa_nome;
    const cor=ident?'#F0A02A':(COR_CLASSE[c.classe]||COR_CLASSE.FACIAL);
    // Contorno escuro por baixo, como nas zonas: a caixa é desenhada sobre
    // o que a câmera vê, e sobre parede branca ou piso claro a cor sozinha
    // some. O vulto não depende do fundo nem da classe.
    ctx.strokeStyle='rgba(0,0,0,0.55)';
    ctx.lineWidth=4.6*e;
    ctx.strokeRect(x,y,w,h);
    ctx.strokeStyle=cor;
    ctx.lineWidth=2.4*e;
    ctx.strokeRect(x,y,w,h);

    const base=ident?c.pessoa_nome:(c.classe?(ROTULO_OBJ[c.classe]||c.classe):'Rosto');
    const pct=c.confianca!=null?` ${Math.round(c.confianca*100)}%`:'';
    // A placa vale mais que a classe: "carro" o operador já vê na imagem.
    const rot=(c.placa?`${base} · ${c.placa}`:`${base}${pct}`).toUpperCase();
    // Tarja também em pixel de tela: com `s` ela encolhia junto com o tile e
    // o texto saía do retângulo que deveria contê-lo.
    const pad=3.5*e, alt=(fonte+4)*e, larg=ctx.measureText(rot).width+pad*2;
    let ty=Math.max(0,y-alt);
    // Atropelou outra tarja: vai para dentro da própria caixa, no alto.
    if(colide(x,ty,larg,alt))ty=y;
    ocupados.push({x,y:ty,w:larg,h:alt});
    ctx.fillStyle=cor;
    ctx.fillRect(x,ty,larg,alt);
    ctx.fillStyle='#08121A';
    ctx.fillText(rot,x+pad,ty+alt/2);
  });
  ctx.restore();
}

/* Recorte guardado da captura: cabe inteiro e não é ampliado além do que a
   resolução aguenta. Esticar um recorte de 49x85 até 500px não cria detalhe
   nenhum — só vira um borrão que parece defeito do sistema. */
function pintarRecorte(ctx,img,W,H){
  const MAX_AMPLIACAO=3;
  const esc=Math.min(W/img.naturalWidth,H/img.naturalHeight,MAX_AMPLIACAO);
  const w=img.naturalWidth*esc,h=img.naturalHeight*esc;
  ctx.fillStyle='#0A1218';
  ctx.fillRect(0,0,W,H);
  ctx.drawImage(img,(W-w)/2,(H-h)/2,w,h);
}

/* Quadro ao vivo cobrindo o tile, sem distorcer: recorta o excedente.

   Com `conter`, nada é recortado — o quadro inteiro cabe na área. É o que a
   ficha da câmera precisa: quem vai ajustar enquadramento tem de ver onde o
   campo de visão termina, e o corte do mosaico escondia justamente as bordas. */
function pintarAoVivo(ctx,img,W,H,conter){
  const esc=conter?Math.min(W/img.naturalWidth,H/img.naturalHeight)
                  :Math.max(W/img.naturalWidth,H/img.naturalHeight);
  const w=img.naturalWidth*esc,h=img.naturalHeight*esc;
  if(conter){ctx.fillStyle='#000';ctx.fillRect(0,0,W,H)}
  ctx.drawImage(img,(W-w)/2,(H-h)/2,w,h);
}

/* ---------- Motor ---------- */
const FEED={
  tiles:[],
  running:false,
  last:0,
  frames:0,
  fpsT:0,
  fps:0,
  aoMedirFps:null,
  mostrarZonas:true,
  mostrarCaixas:true,
  fonteAoVivo:null,    // (cam) => URL do MJPEG, ou null se a câmera não tem
  fonteSnapshot:null,  // (cam) => URL de um quadro único, para pré-visualização
  caixas:new Map(),    // código da câmera => caixas reais ainda vivas

  definirCaixas(codigo,lista,validadeS){
    // Substitui o conjunto inteiro: é a cena atual, não um acúmulo.
    // A validade vem do worker, que é quem sabe quando volta a esta câmera.
    const vida=validadeS?validadeS*1000:CAIXA_VIDA_MS;
    const reg=this.caixas.get(codigo);
    const novas=lista||[];
    // De onde cada caixa parte. O worker analisa um quadro a cada segundo e
    // pouco, não trinta por segundo: sem isto a caixa salta de uma posição
    // para outra, e quem olha vê a marcação piscando de lugar em vez de
    // acompanhar a pessoa. A origem é a posição em que a caixa está *agora*
    // na tela — não a última publicada —, senão uma publicação que chega no
    // meio da transição recomeça o movimento do começo.
    const antes=reg?reg.lista:[];
    novas.forEach(c=>{
      const velha=this.caixaAnterior(antes,c);
      if(velha)c._de=this.posicaoAtual(velha,reg?reg.em:0);
    });
    this.caixas.set(codigo,{ate:Date.now()+vida,em:Date.now(),lista:novas});
  },
  /* A mesma caixa na publicação anterior. Pelo `track_id`, que o worker
     mantém estável entre duas renovações; sem ele não há como afirmar que
     são a mesma pessoa, e deslizar a caixa errada seria pior que não
     deslizar — a marcação atravessaria a cena até alguém que nunca esteve
     ali. Por isso caixa sem identidade simplesmente não desliza.

     A classe entra na comparação porque a tarja do nome leva o `track_id`
     do corpo sob ela: são duas caixas com a mesma identidade, e sem separar
     por classe a do rosto casaria com a do corpo e atravessaria a tela. */
  caixaAnterior(antes,nova){
    if(nova.track_id==null)return null;
    return antes.find(v=>v.track_id===nova.track_id&&v.classe===nova.classe)||null;
  },
  /* Onde esta caixa está desenhada neste instante, já contado o deslize em
     curso. */
  posicaoAtual(c,em){
    const p=this.progresso(em);
    if(!c._de||p>=1)return c.bbox;
    return c.bbox.map((v,i)=>c._de[i]+(v-c._de[i])*p);
  },
  progresso(em){
    if(!em)return 1;
    return Math.min(1,(Date.now()-em)/TRANSICAO_MS);
  },
  caixasDe(codigo){
    const reg=this.caixas.get(codigo);
    // Se o worker parar de publicar, as caixas somem. Melhor nenhuma caixa
    // que uma caixa parada sobre um objeto que já saiu de cena.
    if(!reg||reg.ate<=Date.now())return[];
    const p=this.progresso(reg.em);
    if(p>=1)return reg.lista;
    return reg.lista.map(c=>c._de
      ?{...c,bbox:c.bbox.map((v,i)=>c._de[i]+(v-c._de[i])*p)}
      :c);
  },

  register(canvas,cam,opts){
    const t={canvas,ctx:canvas.getContext('2d',{alpha:false}),cam,opts:opts||{},
             img:null,modo:null,poll:null};
    this.tiles.push(t);
    this.ligarFonte(t);
  },
  /* O MJPEG segura a conexão HTTP aberta enquanto o vídeo durar, e o
     navegador só permite seis por origem. Com o parque maior que isso os
     tiles excedentes ficavam pretos para sempre — a requisição nunca saía da
     fila, e nada no servidor acusava erro porque ela nunca chegou lá.
     Os primeiros ficam no fluxo contínuo; os demais recarregam um quadro por
     vez, em conexões que fecham e devolvem a vaga. */
  ligarFonte(t){
    const emFluxo=this.tiles.filter(x=>x.modo==='mjpeg').length;
    // Com o parque inteiro na tela, cinco fluxos contínuos consomem quase
    // todas as vagas de conexão e deixam uma só para o rodízio atender as
    // outras vinte e uma — uma volta de mais de dois segundos.
    // A ficha e a tela cheia são um tile só, e é onde a fluidez importa.
    if((t.opts&&t.opts.hd)||emFluxo<MAX_MJPEG){
      const url=this.fonteAoVivo?this.fonteAoVivo(t.cam,t.opts):null;
      if(!url)return;
      t.modo='mjpeg';
      const img=new Image();
      img.onerror=()=>this.semFluxo(t);
      img.src=url;
      t.img=img;
      return;
    }
    t.modo='poll';
    this.iniciarRodizio();
  },
  /* O fluxo contínuo falhou — e antes isso deixava o tile preto para sempre.
     Era literalmente o que o código dizia ("câmera muda: o tile fica preto"),
     e com uma câmera só no parque isso bastava para a tela mentir: o
     Elevador leva minutos para negociar o RTSP e, enquanto não negocia, o
     servidor recusa o fluxo. O <img> recebia o erro uma vez, zerava, e nada
     tentava de novo — quando a câmera voltava, minutos depois, o tile
     continuava preto até alguém trocar de página ou recarregar.

     A recuperação é cair no rodízio de snapshots, que já se cura sozinho:
     ele repete o pedido a cada volta e é servido pelo último quadro
     guardado, que sobrevive à reconexão do leitor. Custa a fluidez do fluxo
     contínuo nesse tile, e devolve a vaga de conexão para quem a use. O
     fluxo volta na próxima montagem da grade. */
  semFluxo(t){
    if(t.img){t.img.onerror=null;t.img.onload=null;t.img.src='';t.img=null}
    if(!(t.opts&&t.opts.hd)){
      t.modo='poll';
      this.iniciarRodizio();
      return;
    }
    /* Tela cheia e ficha da câmera são um tile só, e é onde a resolução
       nativa importa — cair no snapshot reduzido ali seria trocar o borrão
       pelo preto. Então reinsiste no próprio fluxo, espaçando as tentativas
       para não martelar uma câmera que ainda está negociando. */
    if(t.reconectar)clearTimeout(t.reconectar);
    t.espera=Math.min((t.espera||RETOMAR_MS)*2,RETOMAR_MAX_MS);
    t.reconectar=setTimeout(()=>{
      t.reconectar=null;
      if(t.canvas&&t.canvas.isConnected){t.modo=null;this.ligarFonte(t)}
    },t.espera);
  },
  /* Um pedido por vez, em rodízio pelos tiles sem fluxo contínuo.
     Um temporizador por tile não resolve: eles disparam juntos e disputam as
     poucas vagas de conexão que sobram depois dos fluxos, e os que perdem a
     disputa ficam pretos enquanto os outros seguem. Em fila, cada pedido
     espera o anterior terminar, então nunca há mais de uma conexão de
     recarga aberta — e como o servidor responde em cerca de 100ms, a volta
     inteira pelo parque leva menos de um segundo. */
  iniciarRodizio(){
    if(this._rodizio)return;
    this._rodizio=setInterval(()=>{
      const fila=this.tiles.filter(t=>t.modo==='poll'&&t.canvas&&t.canvas.isConnected);
      if(!fila.length){clearInterval(this._rodizio);this._rodizio=null;return}
      // Tantos pedidos quantas vagas o fluxo contínuo deixou livres, guardando
      // uma para o WebSocket e as chamadas da própria interface. Um pedido só
      // de cada vez deixava as demais vagas paradas enquanto a fila crescia.
      const emFluxo=this.tiles.filter(x=>x.modo==='mjpeg').length;
      const vagas=Math.max(1,5-emFluxo);
      if((this._puxando||0)>=vagas)return;
      const t=fila[(this._vez||0)%fila.length];
      this._vez=((this._vez||0)+1)%fila.length;
      const url=this.fonteSnapshot?this.fonteSnapshot(t.cam):null;
      if(!url)return;
      this._puxando=(this._puxando||0)+1;
      const img=new Image();
      const liberar=()=>{this._puxando=Math.max(0,(this._puxando||1)-1)};
      // Só troca o quadro exibido quando o novo chegou inteiro: atribuir
      // antes deixaria o tile piscar em branco a cada recarga.
      img.onload=()=>{t.img=img;liberar()};
      img.onerror=liberar;
      img.src=url+'&t='+Date.now();
    },POLL_MS);
  },
  soltar(t){
    if(t.img){t.img.onerror=null;t.img.onload=null;t.img.src='';t.img=null}
    // A tentativa agendada morre com o tile: sem isto, um modal fechado no
    // meio da espera voltaria a abrir o fluxo da câmera minutos depois, e o
    // leitor RTSP do servidor seguiria vivo alimentando ninguém.
    if(t.reconectar){clearTimeout(t.reconectar);t.reconectar=null}
    t.espera=0;
    t.modo=null;
  },
  /* Tile cujo canvas saiu do documento — modal fechado, lista redesenhada.
     Sem soltar o `src`, a conexão MJPEG continua aberta e o leitor RTSP do
     servidor segue vivo, alimentando um canvas que ninguém vê. Na ficha da
     câmera isso custava caro: o fluxo em resolução nativa não terminava. */
  podar(){
    const vivos=[];
    this.tiles.forEach(t=>{
      if(t.canvas&&t.canvas.isConnected){vivos.push(t);return}
      this.soltar(t);
    });
    this.tiles=vivos;
  },
  clear(){
    // Soltar o src encerra a conexão MJPEG. Sem isto cada troca de tela
    // deixaria um leitor RTSP pendurado no servidor.
    this.tiles.forEach(t=>this.soltar(t));
    this.tiles=[];
    if(this._rodizio){clearInterval(this._rodizio);this._rodizio=null}
    this._puxando=0;this._vez=0;
  },
  resize(t){
    const r=t.canvas.getBoundingClientRect();
    if(!r.width||!r.height)return false;
    const dpr=Math.min(window.devicePixelRatio||1,2);
    // 900px é o teto do mosaico, onde cada tile é pequeno e são muitos. Na
    // ficha da câmera esse mesmo teto era o gargalo final: o quadro nativo
    // chegava inteiro e era reamostrado para 900 antes de aparecer.
    const teto=t.opts.hd?2560:900;
    const w=Math.min(Math.round(r.width*dpr),teto), h=Math.round(w*r.height/r.width);
    if(t.canvas.width!==w||t.canvas.height!==h){t.canvas.width=w;t.canvas.height=h}
    /* Quantos pixels de canvas cabem num pixel de tela. Tudo o que é
       desenhado aqui está em pixels de canvas e depois é reduzido para
       o tamanho do tile: no mosaico 4x4 o canvas vai a 640 e aparece em
       320, então uma borda de 2 vira 1 real e a fonte de 9,5 vira 4,75.
       Era por isso que a caixa sumia no mosaico e aparecia no 1x1. */
    t.escala=w/r.width;
    return true;
  },
  loop(ts){
    if(!FEED.running)return;
    requestAnimationFrame(FEED.loop);
    const dt=Math.min((ts-FEED.last)/1000,.12);
    if(dt<1/22)return;
    FEED.last=ts;
    FEED.frames++;
    if(ts-FEED.fpsT>1000){FEED.fps=FEED.frames;FEED.frames=0;FEED.fpsT=ts;if(FEED.aoMedirFps)FEED.aoMedirFps(FEED.fps)}
    if(document.hidden)return;
    FEED.podar();

    FEED.tiles.forEach(t=>{
      if(!FEED.resize(t))return;
      const ctx=t.ctx,W=t.canvas.width,H=t.canvas.height;
      const vivo=t.img&&t.img.naturalWidth>0;
      // Mostra vídeo real quando carregado; enquanto carrega, canvas fica escuro
      if(vivo){
        pintarAoVivo(ctx,t.img,W,H,t.opts.conter);
        if(t.opts.zonas!==false&&FEED.mostrarZonas)drawZonas(ctx,t.cam,W,H,t.escala);
      }else{
        ctx.fillStyle='#0A0E15';
        ctx.fillRect(0,0,W,H);
      }
      // Caixas só sobre imagem: num tile preto, a caixa de "carro" flutuando
      // no vazio descrevia uma cena que ninguém estava vendo.
      if(vivo&&t.opts.boxes!==false&&FEED.mostrarCaixas)pintarCaixasReais(ctx,FEED.caixasDe(t.cam.codigo),W,H,t.escala);
    });
  },
  start(){if(this.running)return;this.running=true;this.last=performance.now();requestAnimationFrame(this.loop)},
  stop(){this.running=false}
};

/* ---------- Retrato biométrico (template, não fotografia) ---------- */
function drawBioFace(canvas,seed,sexo,size){
  const dpr=Math.min(window.devicePixelRatio||1,2);
  const S=size||44;
  canvas.width=S*dpr;canvas.height=S*dpr;
  canvas.style.width=S+'px';canvas.style.height=S+'px';
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const r=mulberry(seed>>>0);
  const cor=SEXOS[sexo]?SEXOS[sexo].cor:'#5F7589';
  const g=ctx.createLinearGradient(0,0,S,S);
  g.addColorStop(0,'#0E1821');g.addColorStop(1,'#16232E');
  ctx.fillStyle=g;ctx.fillRect(0,0,S,S);
  const cx=S/2, cy=S*.54, hw=S*.24+r()*S*.03, hh=S*.31+r()*S*.03;
  ctx.save();
  ctx.beginPath();ctx.ellipse(cx,cy,hw,hh,0,0,6.3);
  ctx.fillStyle='rgba(255,255,255,.05)';ctx.fill();
  ctx.strokeStyle=cor;ctx.globalAlpha=.55;ctx.lineWidth=.9;ctx.stroke();ctx.globalAlpha=1;
  // malha de landmarks
  const pts=[];
  for(let i=0;i<7;i++){const a=Math.PI*(.15+i*.115);pts.push([cx-Math.cos(a)*hw*.93,cy+Math.sin(a)*hh*.9-hh*.1])}
  for(let i=0;i<7;i++){const a=Math.PI*(.15+i*.115);pts.push([cx+Math.cos(a)*hw*.93,cy+Math.sin(a)*hh*.9-hh*.1])}
  pts.push([cx-hw*.42,cy-hh*.22],[cx+hw*.42,cy-hh*.22]);           // olhos
  pts.push([cx,cy+hh*.06],[cx-hw*.13,cy+hh*.14],[cx+hw*.13,cy+hh*.14]); // nariz
  pts.push([cx-hw*.3,cy+hh*.42],[cx,cy+hh*.47],[cx+hw*.3,cy+hh*.42]);   // boca
  pts.push([cx-hw*.55,cy-hh*.5],[cx+hw*.55,cy-hh*.5],[cx,cy-hh*.62]);   // testa
  ctx.strokeStyle=cor;ctx.globalAlpha=.30;ctx.lineWidth=.55;
  for(let i=0;i<pts.length;i++){
    for(let j=i+1;j<pts.length;j++){
      const dx=pts[i][0]-pts[j][0],dy=pts[i][1]-pts[j][1];
      if(Math.hypot(dx,dy)<S*.23){ctx.beginPath();ctx.moveTo(pts[i][0],pts[i][1]);ctx.lineTo(pts[j][0],pts[j][1]);ctx.stroke()}
    }
  }
  ctx.globalAlpha=1;ctx.fillStyle=cor;
  pts.forEach(p=>{ctx.beginPath();ctx.arc(p[0],p[1],S*.018,0,6.3);ctx.fill()});
  ctx.restore();
  // cantoneiras
  ctx.strokeStyle=cor;ctx.globalAlpha=.75;ctx.lineWidth=1;
  const m=S*.09,c=S*.14;
  ctx.beginPath();
  ctx.moveTo(m,m+c);ctx.lineTo(m,m);ctx.lineTo(m+c,m);
  ctx.moveTo(S-m-c,m);ctx.lineTo(S-m,m);ctx.lineTo(S-m,m+c);
  ctx.moveTo(S-m,S-m-c);ctx.lineTo(S-m,S-m);ctx.lineTo(S-m-c,S-m);
  ctx.moveTo(m+c,S-m);ctx.lineTo(m,S-m);ctx.lineTo(m,S-m-c);
  ctx.stroke();ctx.globalAlpha=1;
}

